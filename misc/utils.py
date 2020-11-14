import os
import shutil  # High-level file operations
import random
import numpy as np


def color_mask(a, r, g, b):
    ch_r = a[..., 0] == r
    ch_g = a[..., 1] == g
    ch_b = a[..., 2] == b
    return ch_r & ch_g & ch_b


def normalize(mask, dtype=np.uint8):
    return (255 * mask / np.amax(mask)).astype(dtype)


def bounding_box(img):
    rows = np.any(img, axis=1)
    cols = np.any(img, axis=0)
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    return rmin, rmax, cmin, cmax


def cropping_center(x, crop_shape, batch=False):
    orig_shape = x.shape
    if not batch:
        h0 = int((orig_shape[0] - crop_shape[0]) * 0.5)
        w0 = int((orig_shape[1] - crop_shape[1]) * 0.5)
        x = x[h0:h0 + crop_shape[0], w0:w0 + crop_shape[1]]
    else:
        h0 = int((orig_shape[1] - crop_shape[0]) * 0.5)
        w0 = int((orig_shape[2] - crop_shape[1]) * 0.5)
        x = x[:, h0:h0 + crop_shape[0], w0:w0 + crop_shape[1]]
    return x


# to make it easier for visualization
def randomize_label(label_map):
    label_list = np.unique(label_map)
    label_list = label_list[1:]  # exclude the background
    label_rand = list(label_list)  # dup frist cause shuffle is done in place
    random.shuffle(label_rand)
    new_map = np.zeros(label_map.shape, dtype=label_map.dtype)


"""Recursive directory creation function. Like mkdir(), 
but makes all intermediate-level directories needed to contain the leaf directory.
A leaf is a node on a tree with no child nodes."""


def rm_n_mkdir(dir):
    if os.path.isdir(dir):
        shutil.rmtree(dir)
    os.makedirs(dir)


###
# test

# import cv2
# import matplotlib.pyplot as plt
#
# img = cv2.imread('/media/vtltrinh/Data1/COLON_MANUAL_PATCHES/v1/1010711/000_3.jpg')
# im = np.array(img)
# im_mask = color_mask(im, 1, 1, 1)
#
# bound = bounding_box(im)
# print(bound)
