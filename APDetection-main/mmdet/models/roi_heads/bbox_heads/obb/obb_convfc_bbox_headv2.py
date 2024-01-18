import torch
import torch.nn as nn
from mmcv.cnn import ConvModule

from mmdet.models.builder import HEADS
from .obbox_headv2 import OBBoxHeadv2


@HEADS.register_module()
class OBBConvFCBBoxHeadv2(OBBoxHeadv2):
    r"""More general bbox head, with shared conv and fc layers and two optional
    separated branches.

    .. code-block:: none

                                    /-> cls convs -> cls fcs -> cls
        shared convs -> shared fcs
                                    \-> reg convs -> reg fcs -> reg
    """  # noqa: W605

    def __init__(self,
                 num_shared_convs=0,
                 num_shared_fcs=0,
                 num_cls_convs=0,
                 num_cls_fcs=0,
                 num_reg_convs=0,
                 num_reg_fcs=0,
                 conv_out_channels=256,
                 fc_out_channels=1024,
                 conv_cfg=None,
                 norm_cfg=None,
                 *args,
                 **kwargs):
        super(OBBConvFCBBoxHeadv2, self).__init__(*args, **kwargs)
        assert (num_shared_convs + num_shared_fcs + num_cls_convs +
                num_cls_fcs + num_reg_convs + num_reg_fcs > 0)
        if num_cls_convs > 0 or num_reg_convs > 0:
            assert num_shared_fcs == 0
        if not self.with_cls:
            assert num_cls_convs == 0 and num_cls_fcs == 0
        if not self.with_reg:
            assert num_reg_convs == 0 and num_reg_fcs == 0
        self.num_shared_convs = num_shared_convs
        self.num_shared_fcs = num_shared_fcs
        self.num_cls_convs = num_cls_convs
        self.num_cls_fcs = num_cls_fcs
        self.num_reg_convs = num_reg_convs
        self.num_reg_fcs = num_reg_fcs
        self.conv_out_channels = conv_out_channels
        self.fc_out_channels = fc_out_channels
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg

        # add shared convs and fcs
        self.shared_convs, self.shared_fcs, last_layer_dim = \
            self._add_conv_fc_branch(
                self.num_shared_convs, self.num_shared_fcs, self.in_channels,
                True)
        self.shared_out_channels = last_layer_dim

        # add cls specific branch
        self.cls_convs, self.cls_fcs, self.cls_last_dim = \
            self._add_conv_fc_branch(
                self.num_cls_convs, self.num_cls_fcs, self.shared_out_channels)

        # add reg specific branch
        self.reg_convs, self.reg_fcs, self.reg_last_dim = \
            self._add_conv_fc_branch(
                self.num_reg_convs, self.num_reg_fcs, self.shared_out_channels)

        # add bpts specific branch
        self.bpts_convs, self.bpts_fcs, self.bpts_last_dim = \
            self._add_conv_fc_branch(
                self.num_reg_convs, self.num_reg_fcs, self.shared_out_channels)

        if self.num_shared_fcs == 0 and not self.with_avg_pool:
            if self.num_cls_fcs == 0:
                self.cls_last_dim *= self.roi_feat_area
            if self.num_reg_fcs == 0:
                self.reg_last_dim *= self.roi_feat_area

        self.relu = nn.ReLU(inplace=True)
        # reconstruct fc_cls and fc_reg since input channels are changed
        if self.with_cls:
            self.fc_cls = nn.Linear(self.cls_last_dim, self.num_classes + 1)
        if self.with_reg:
            out_dim_reg = self.reg_dim if self.reg_class_agnostic else \
                    self.reg_dim * self.num_classes
            self.fc_reg = nn.Linear(self.reg_last_dim, out_dim_reg)
        if self.with_bpts:
            out_dim_bpts = self.reg_dim if self.reg_class_agnostic else \
                    self.reg_dim * self.num_classes
            self.fc_bpts = nn.Linear(self.bpts_last_dim, out_dim_bpts)

    def _add_conv_fc_branch(self,
                            num_branch_convs,
                            num_branch_fcs,
                            in_channels,
                            is_shared=False):
        """Add shared or separable branch

        convs -> avg pool (optional) -> fcs
        """
        last_layer_dim = in_channels
        # add branch specific conv layers
        branch_convs = nn.ModuleList()
        if num_branch_convs > 0:
            for i in range(num_branch_convs):
                conv_in_channels = (
                    last_layer_dim if i == 0 else self.conv_out_channels)
                branch_convs.append(
                    ConvModule(
                        conv_in_channels,
                        self.conv_out_channels,
                        3,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg))
            last_layer_dim = self.conv_out_channels
        # add branch specific fc layers
        branch_fcs = nn.ModuleList()
        if num_branch_fcs > 0:
            # for shared branch, only consider self.with_avg_pool
            # for separated branches, also consider self.num_shared_fcs
            if (is_shared
                    or self.num_shared_fcs == 0) and not self.with_avg_pool:
                last_layer_dim *= self.roi_feat_area
            for i in range(num_branch_fcs):
                fc_in_channels = (
                    last_layer_dim if i == 0 else self.fc_out_channels)
                branch_fcs.append(
                    nn.Linear(fc_in_channels, self.fc_out_channels))
            last_layer_dim = self.fc_out_channels
        return branch_convs, branch_fcs, last_layer_dim

    def init_weights(self):
        super(OBBConvFCBBoxHeadv2, self).init_weights()
        # conv layers are already initialized by ConvModule
        for module_list in [self.shared_fcs, self.cls_fcs, self.reg_fcs, self.bpts_fcs]:
            for m in module_list.modules():
                if isinstance(m, nn.Linear):
                    nn.init.xavier_uniform_(m.weight)
                    nn.init.constant_(m.bias, 0)

    def forward(self, x):
        # separate attention features by 256 channels
        # x1 = x[:, 0:256, :, :]
        # x2 = x[:, 256:512, :, :]
        x1 = torch.add(x, x, alpha=0.3)
        x2 = torch.add(x1, x, alpha=0.3)

        # shared part
        if self.num_shared_convs > 0:
            for conv in self.shared_convs:
                x = conv(x)
                x1 = conv(x1)
                x2 = conv(x2)

        if self.num_shared_fcs > 0:
            if self.with_avg_pool:
                x = self.avg_pool(x)
                x1 = self.avg_pool(x1)
                x2 = self.avg_pool(x2)

            x = x.flatten(1)
            x1 = x1.flatten(1)
            x2 = x2.flatten(1)

            for fc in self.shared_fcs:
                x = self.relu(fc(x))
                x1 = self.relu(fc(x1))
                x2 = self.relu(fc(x2))

        # separate branches
        x_cls = x
        x_reg = x
        x_bpts1 = x1
        x_bpts2 = x2

        # classification prediction
        for conv in self.cls_convs:
            x_cls = conv(x_cls)
        if x_cls.dim() > 2:
            if self.with_avg_pool:
                x_cls = self.avg_pool(x_cls)
            x_cls = x_cls.flatten(1)
        for fc in self.cls_fcs:
            x_cls = self.relu(fc(x_cls))
        cls_score = self.fc_cls(x_cls) if self.with_cls else None

        # bounding box prediction
        for conv in self.reg_convs:
            x_reg = conv(x_reg)
        if x_reg.dim() > 2:
            if self.with_avg_pool:
                x_reg = self.avg_pool(x_reg)
            x_reg = x_reg.flatten(1)
        for fc in self.reg_fcs:
            x_reg = self.relu(fc(x_reg))
        bbox_pred = self.fc_reg(x_reg) if self.with_reg else None

        # box points prediction 
        for conv in self.bpts_convs:
            x_bpts1 = conv(x_bpts1)
            x_bpts2 = conv(x_bpts2)
        if x_bpts1.dim() > 2 or x_bpts2.dim() > 2:
            if self.with_avg_pool:
                x_bpts1 = self.avg_pool(x_bpts1)
                x_bpts2 = self.avg_pool(x_bpts2)
            x_bpts1 = x_bpts1.flatten(1)
            x_bpts2 = x_bpts2.flatten(1)
        for fc in self.bpts_fcs:
            x_bpts1 = self.relu(fc(x_bpts1))
            x_bpts2 = self.relu(fc(x_bpts2))
        bpts_pred1 = self.fc_bpts(x_bpts1) if self.with_bpts else None
        bpts_pred2 = self.fc_bpts(x_bpts2) if self.with_bpts else None
        bpts_pred = torch.cat((bpts_pred1, bpts_pred2), 1)

        # return the predictions
        return cls_score, bbox_pred, bpts_pred
        


@HEADS.register_module()
class OBBShared2FCBBoxHeadv2(OBBConvFCBBoxHeadv2):

    def __init__(self, fc_out_channels=1024, *args, **kwargs):
        super(OBBShared2FCBBoxHeadv2, self).__init__(
            num_shared_convs=0,
            num_shared_fcs=2,
            num_cls_convs=0,
            num_cls_fcs=0,
            num_reg_convs=0,
            num_reg_fcs=0,
            fc_out_channels=fc_out_channels,
            *args,
            **kwargs)


@HEADS.register_module()
class OBBShared4Conv1FCBBoxHeadv2(OBBConvFCBBoxHeadv2):

    def __init__(self, fc_out_channels=1024, *args, **kwargs):
        super(OBBShared4Conv1FCBBoxHeadv2, self).__init__(
            num_shared_convs=4,
            num_shared_fcs=1,
            num_cls_convs=0,
            num_cls_fcs=0,
            num_reg_convs=0,
            num_reg_fcs=0,
            fc_out_channels=fc_out_channels,
            *args,
            **kwargs)