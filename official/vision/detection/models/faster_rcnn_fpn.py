# -*- coding:utf-8 -*-
# MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
#
# Copyright (c) 2014-2020 Megvii Inc. All rights reserved.
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
import numpy as np
import megengine as mge
import megengine.functional as F
import megengine.module as M

from official.vision.classification.resnet.model import resnet50
from official.vision.detection import layers


class FasterRCNN(M.Module):

    def __init__(self, cfg, batch_size):
        super().__init__()
        self.cfg = cfg
        cfg.batch_per_gpu = batch_size
        self.batch_size = batch_size
        # ----------------------- build the backbone ------------------------ #
        bottom_up = resnet50(norm=layers.get_norm(cfg.resnet_norm))

        # ------------ freeze the weights of resnet stage1 and stage 2 ------ #
        if self.cfg.backbone_freeze_at >= 1:
            for p in bottom_up.conv1.parameters():
                p.requires_grad = False
        if self.cfg.backbone_freeze_at >= 2:
            for p in bottom_up.layer1.parameters():
                p.requires_grad = False

        # -------------------------- build the FPN -------------------------- #
        out_channels = 256
        self.backbone = layers.FPN(
            bottom_up=bottom_up,
            in_features=["res2", "res3", "res4", "res5"],
            out_channels=out_channels,
            norm="",
            top_block=layers.FPNP6(),
            strides=[4, 8, 16, 32],
            channels=[256, 512, 1024, 2048],
        )

        # -------------------------- build the RPN -------------------------- #
        self.RPN = layers.RPN(cfg)

        # ----------------------- build the RCNN head ----------------------- #
        self.RCNN = layers.RCNN(cfg)

        # -------------------------- input Tensor --------------------------- #
        self.inputs = {
            "image": mge.tensor(
                np.random.random([2, 3, 224, 224]).astype(np.float32), dtype="float32",
            ),
            "im_info": mge.tensor(
                np.random.random([2, 5]).astype(np.float32), dtype="float32",
            ),
            "gt_boxes": mge.tensor(
                np.random.random([2, 100, 5]).astype(np.float32), dtype="float32",
            ),
        }

    def preprocess_image(self, image):
        normed_image = (
            image - self.cfg.img_mean[None, :, None, None]
        ) / self.cfg.img_std[None, :, None, None]
        return layers.get_padded_tensor(normed_image, 32, 0.0)

    def forward(self, inputs):
        images = inputs['image']
        im_info = inputs['im_info']
        gt_boxes = inputs['gt_boxes']
        # process the images
        normed_images = self.preprocess_image(images)
        # normed_images = images
        fpn_features = self.backbone(normed_images)

        if self.training:
            return self._forward_train(fpn_features, im_info, gt_boxes)
        else:
            return self.inference(fpn_features, im_info)

    def _forward_train(self, fpn_features, im_info, gt_boxes):
        rpn_rois, rpn_losses = self.RPN(fpn_features, im_info, gt_boxes)
        rcnn_losses = self.RCNN(fpn_features, rpn_rois, im_info, gt_boxes)

        loss_rpn_cls = rpn_losses['loss_rpn_cls']
        loss_rpn_loc = rpn_losses['loss_rpn_loc']
        loss_rcnn_cls = rcnn_losses['loss_rcnn_cls']
        loss_rcnn_loc = rcnn_losses['loss_rcnn_loc']
        total_loss = loss_rpn_cls + loss_rpn_loc + loss_rcnn_cls + loss_rcnn_loc

        loss_dict = {
            "total_loss": total_loss,
            "rpn_cls": loss_rpn_cls,
            "rpn_loc": loss_rpn_loc,
            "rcnn_cls": loss_rcnn_cls,
            "rcnn_loc": loss_rcnn_loc
        }
        self.cfg.losses_keys = list(loss_dict.keys())
        return loss_dict

    def inference(self, fpn_features, im_info):
        rpn_rois = self.RPN(fpn_features, im_info)
        pred_boxes, pred_score = self.RCNN(fpn_features, rpn_rois)
        # pred_score = pred_score[:, None]
        pred_boxes = pred_boxes.reshape(-1, 4)
        scale_w = im_info[0, 1] / im_info[0, 3]
        scale_h = im_info[0, 0] / im_info[0, 2]
        pred_boxes = pred_boxes / F.concat(
            [scale_w, scale_h, scale_w, scale_h], axis=0
        )

        clipped_boxes = layers.get_clipped_box(
            pred_boxes, im_info[0, 2:4]
        ).reshape(-1, self.cfg.num_classes, 4)
        return pred_score, clipped_boxes


class FasterRCNNConfig:

    def __init__(self):
        self.resnet_norm = "FrozenBN"
        self.backbone_freeze_at = 2

        # ------------------------ data cfg --------------------------- #
        self.train_dataset = dict(
            name="coco",
            root="train2017",
            ann_file="annotations/instances_train2017.json",
        )
        self.test_dataset = dict(
            name="coco",
            root="val2017",
            ann_file="annotations/instances_val2017.json",
        )
        self.num_classes = 80

        self.img_mean = np.array([103.530, 116.280, 123.675])  # BGR
        self.img_std = np.array([57.375, 57.120, 58.395])

        # ----------------------- rpn cfg ------------------------- #
        self.anchor_base_size = 16
        self.anchor_scales = np.array([0.5])
        self.anchor_aspect_ratios = [0.5, 1, 2]
        self.anchor_offset = -0.5
        self.num_cell_anchors = len(self.anchor_aspect_ratios)

        self.bbox_normalize_means = None
        self.bbox_normalize_stds = np.array([0.1, 0.1, 0.2, 0.2])

        self.rpn_stride = np.array([4, 8, 16, 32, 64]).astype(np.float32)
        self.rpn_in_features = ["p2", "p3", "p4", "p5", "p6"]
        self.rpn_channel = 256

        self.rpn_nms_threshold = 0.7
        self.allow_low_quality = True
        self.num_sample_anchors = 256
        self.positive_anchor_ratio = 0.5
        self.rpn_positive_overlap = 0.7
        self.rpn_negative_overlap = 0.3
        self.ignore_label = -1

        # ----------------------- rcnn cfg ------------------------- #
        self.pooling_method = 'roi_align'
        self.pooling_size = (7, 7)

        self.num_rois = 512
        self.fg_ratio = 0.5
        self.fg_threshold = 0.5
        self.bg_threshold_high = 0.5
        self.bg_threshold_low = 0.0

        self.rcnn_in_features = ["p2", "p3", "p4", "p5"]
        self.rcnn_stride = [4, 8, 16, 32]

        # ------------------------ loss cfg -------------------------- #
        self.rpn_smooth_l1_beta = 3
        self.rcnn_smooth_l1_beta = 1

        # ------------------------ training cfg ---------------------- #
        self.train_image_short_size = 800
        self.train_image_max_size = 1333
        self.train_prev_nms_top_n = 2000
        self.train_post_nms_top_n = 1000

        self.num_losses = 5
        self.basic_lr = 0.02 / 16.0  # The basic learning rate for single-image
        self.momentum = 0.9
        self.weight_decay = 1e-4
        self.log_interval = 20
        self.nr_images_epoch = 80000
        self.max_epoch = 18
        self.warm_iters = 500
        self.lr_decay_rate = 0.1
        self.lr_decay_sates = [12, 16, 17]

        # ------------------------ testing cfg ------------------------- #
        self.test_image_short_size = 800
        self.test_image_max_size = 1333
        self.test_prev_nms_top_n = 1000
        self.test_post_nms_top_n = 1000
        self.test_max_boxes_per_image = 100

        self.test_vis_threshold = 0.3
        self.test_cls_threshold = 0.05
        self.test_nms = 0.5
        self.class_aware_box = True
