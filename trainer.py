import numpy as np
import matplotlib.pyplot as plt

import shutil
import argparse
import os
import json
import random
import warnings
from termcolor import colored
import pandas as pd
from sklearn.metrics import confusion_matrix

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
import torch.utils.data as data

from ignite.contrib.handlers import ProgressBar
from ignite.engine import Engine, Events
from ignite.handlers import ModelCheckpoint, Timer
from ignite.metrics import RunningAverage
from tensorboardX import SummaryWriter

import imgaug  # https://github.com/aleju/imgaug
from imgaug import augmenters as iaa

from misc.utils import *
from misc.train_utils_regres import *

import importlib

import dataset
from config import Config

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0,1'

####

class Trainer(Config):
    ####
    def view_dataset(self, mode='train'):
        train_pairs, valid_pairs = getattr(dataset, ('prepare_%s_data' % self.dataset))()
        if mode == 'train':
            train_augmentors = self.train_augmentors()
            ds = dataset.DatasetSerial(train_pairs, has_aux=False,
                                       shape_augs=iaa.Sequential(train_augmentors[0]),
                                       input_augs=iaa.Sequential(train_augmentors[1]))
        else:
            infer_augmentors = self.infer_augmentors()  # HACK
            ds = dataset.DatasetSerial(valid_pairs, has_aux=False,
                                       shape_augs=iaa.Sequential(infer_augmentors)[0])
        dataset.visualize(ds, 4)
        return

    ####
    def train_step(self, net, batch, optimizer, device):
        net.train()  # train mode

        imgs_cpu, true_cpu = batch
        imgs_cpu = imgs_cpu.permute(0, 3, 1, 2)  # to NCHW

        # push data to GPUs
        imgs = imgs_cpu.to(device).float()
        true = true_cpu.to(device).long()  # not one-hot

        # -----------------------------------------------------------
        net.zero_grad()  # not rnn so not accumulate
        out_net = net(imgs)  # a list contains all the out put of the network
        loss = 0.

        # assign output
        if "CLASS" in self.task_type:
            logit_class = out_net
        if "REGRESS" in self.task_type:
            logit_regress = out_net
        if "MULTI" in self.task_type:
            logit_class, logit_regress = out_net[0], out_net[1]

        # compute loss function
        if "ce" in self.loss_type:
            prob = F.softmax(logit_class, dim=-1)
            loss_entropy = F.cross_entropy(logit_class, true, reduction='mean')
            pred = torch.argmax(prob, dim=-1)
            loss += loss_entropy
        if "mse" in self.loss_type:
            criterion = torch.nn.MSELoss()
            loss_regres = criterion(logit_regress, true.float())
            loss += loss_regres
        if "mae" in self.loss_type:
            criterion = torch.nn.L1Loss()
            loss_regres = criterion(logit_regress, true.float())
            loss += loss_regres
        if "REGRESS" in self.task_type:
            label = torch.tensor([0., 1., 2., 3.]).repeat(len(true), 1).permute(1, 0).cuda()
            pred = torch.argmin(torch.abs(logit_regress - label), 0)

        acc = torch.mean((pred == true).float())  # batch accuracy
        # gradient update
        loss.backward()
        optimizer.step()

        # -----------------------------------------------------------
        return dict(
            loss=loss.item(),
            acc=acc.item(),
        )

    ####
    def infer_step(self, net, batch, device):
        net.eval()  # infer mode

        imgs, true = batch
        imgs = imgs.permute(0, 3, 1, 2)  # to NCHW

        # push data to GPUs and convert to float32
        imgs = imgs.to(device).float()
        true = true.to(device).long()  # not one-hot

        # -----------------------------------------------------------
        with torch.no_grad():  # dont compute gradient
            logit_class, logit_regres = net(imgs)  # forward

            # class operations
            prob = F.softmax(logit_class, dim=-1)
            pred = torch.argmax(prob, dim=-1)

            # regression operations
            logit_regres = logit_regres[:, 0]
            criterion = torch.nn.MSELoss()
            loss_MSE = criterion(logit_regres, true.float()).to(torch.float64)

            return dict(logit=logit_regres.cpu().numpy(),
                        pred=pred.cpu().numpy(),
                        true=true.cpu().numpy(),
                        val_loss=loss_MSE.cpu().numpy())

    ####
    def run_once(self, fold_idx):

        log_dir = self.log_dir
        check_manual_seed(self.seed)
        train_pairs, valid_pairs = getattr(dataset, ('prepare_%s_data' % self.dataset))(fold_idx)

        # --------------------------- Dataloader

        train_augmentors = self.train_augmentors()
        train_dataset = dataset.DatasetSerial(train_pairs[:], has_aux=False,
                                              shape_augs=iaa.Sequential(train_augmentors[0]),
                                              input_augs=iaa.Sequential(train_augmentors[1]))

        infer_augmentors = self.infer_augmentors()  # HACK at has_aux
        infer_dataset = dataset.DatasetSerial(valid_pairs[:], has_aux=False,
                                              shape_augs=iaa.Sequential(infer_augmentors[0]))

        train_loader = data.DataLoader(train_dataset,
                                       num_workers=self.nr_procs_train,
                                       batch_size=self.train_batch_size,
                                       shuffle=True, drop_last=True)

        valid_loader = data.DataLoader(infer_dataset,
                                       num_workers=self.nr_procs_valid,
                                       batch_size=self.infer_batch_size,
                                       shuffle=True, drop_last=False)

        # --------------------------- Training Sequence

        if self.logging:
            check_log_dir(log_dir)

        device = 'cuda'

        # networks
        if "CLASS" in self.task_type:
            net_def = importlib.import_module('model.class_dense')  # dynamic import
        if "REGRESS" in self.task_type:
            net_def = importlib.import_module('model.regres_dense')  # dynamic import
        if "MULTI" in self.task_type:
            net_def = importlib.import_module('model.multitask_net2')  # dynamic import
        net = net_def.densenet121()

        # load pre-trained models
        if self.load_network:
            saved_state = torch.load(self.save_net_path)
            net.load_state_dict(saved_state)

        net = torch.nn.DataParallel(net).to(device)

        # optimizers
        optimizer = optim.Adam(net.parameters(), lr=self.init_lr)
        scheduler = optim.lr_scheduler.StepLR(optimizer, self.lr_steps)

        #
        trainer = Engine(lambda engine, batch: self.train_step(net, batch, optimizer, device))
        valider = Engine(lambda engine, batch: self.infer_step(net, batch, device))

        infer_output = ['logit', 'pred', 'true', 'val_loss']
        ##

        if self.logging:
            checkpoint_handler = ModelCheckpoint(log_dir, self.chkpts_prefix,
                                                 save_interval=1, n_saved=30, require_empty=False)
            # adding handlers using `trainer.add_event_handler` method API
            trainer.add_event_handler(event_name=Events.EPOCH_COMPLETED, handler=checkpoint_handler,
                                      to_save={'net': net})

        timer = Timer(average=True)
        timer.attach(trainer, start=Events.EPOCH_STARTED, resume=Events.ITERATION_STARTED,
                     pause=Events.ITERATION_COMPLETED, step=Events.ITERATION_COMPLETED)
        timer.attach(valider, start=Events.EPOCH_STARTED, resume=Events.ITERATION_STARTED,
                     pause=Events.ITERATION_COMPLETED, step=Events.ITERATION_COMPLETED)

        # attach running average metrics computation
        # decay of EMA to 0.95 to match tensorpack default
        # TODO: refactor this
        RunningAverage(alpha=0.95, output_transform=lambda x: x['acc']).attach(trainer, 'acc')
        RunningAverage(alpha=0.95, output_transform=lambda x: x['loss']).attach(trainer, 'loss')

        # attach progress bar
        pbar = ProgressBar(persist=True)
        pbar.attach(trainer, metric_names=['loss'])
        pbar.attach(valider)

        # adding handlers using `trainer.on` decorator API
        @trainer.on(Events.EXCEPTION_RAISED)
        def handle_exception(engine, e):
            if isinstance(e, KeyboardInterrupt) and (engine.state.iteration > 1):
                engine.terminate()
                warnings.warn('KeyboardInterrupt caught. Exiting gracefully.')
                checkpoint_handler(engine, {'net_exception': net})
            else:
                raise e

        # writer for tensorboard logging
        tfwriter = None  # HACK temporary
        if self.logging:
            tfwriter = SummaryWriter(logdir=log_dir)
            json_log_file = log_dir + '/stats.json'
            with open(json_log_file, 'w') as json_file:
                json.dump({}, json_file)  # create empty file

        ### TODO refactor again
        log_info_dict = {
            'logging': self.logging,
            'optimizer': optimizer,
            'tfwriter': tfwriter,
            'json_file': json_log_file if self.logging else None,
            'nr_classes': self.nr_classes,
            'metric_names': infer_output,
            'infer_batch_size': self.infer_batch_size  # too cumbersome
        }
        trainer.add_event_handler(Events.EPOCH_STARTED, lambda engine: scheduler.step())  # to change the lr
        trainer.add_event_handler(Events.EPOCH_COMPLETED, log_train_ema_results, log_info_dict)
        trainer.add_event_handler(Events.EPOCH_COMPLETED, inference, valider, valid_loader, log_info_dict)
        valider.add_event_handler(Events.ITERATION_COMPLETED, accumulate_outputs)

        # Setup is done. Now let's run the training
        trainer.run(train_loader, self.nr_epochs)
        return

    ####
    def run(self):
        if self.cross_valid:
            for fold_idx in range(0, trainer.nr_fold):
                trainer.run_once(fold_idx)
        else:
            self.run_once(self.fold_idx)
        return


####
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--view', default=False, help='view dataset', action='store_true')
    parser.add_argument('--run_infor', default='multi_ce_mse', help='class_ce, regress_mae, regress_mse, '
                                                                    'multi_ce_mse, multi_ce_mae')
    args = parser.parse_args()

    trainer = Trainer()
    if args.view:
        trainer.view_dataset()
        exit()
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    trainer.run()

