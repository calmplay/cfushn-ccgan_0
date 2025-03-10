"""
https://github.com/christiancosgrove/pytorch-spectral-normalization-gan

chainer: https://github.com/pfnet-research/sngan_projection
"""

import numpy as np
import torch
from torch import nn
from torch.nn.utils import spectral_norm

from config import cfg

channels = 3
bias = True
device = cfg.device


######################################################################################################################
# Generator
class ConditionalBatchNorm2d(nn.Module):
    def __init__(self, num_features, dim_embed):
        super().__init__()
        self.num_features = num_features
        self.bn = nn.BatchNorm2d(num_features, affine=False)
        self.embed_gamma = nn.Linear(dim_embed, num_features, bias=False)
        self.embed_beta = nn.Linear(dim_embed, num_features, bias=False)

    def forward(self, x, y):
        out = self.bn(x)
        gamma = self.embed_gamma(y).view(-1, self.num_features, 1, 1)
        beta = self.embed_beta(y).view(-1, self.num_features, 1, 1)
        out = out + out * gamma + beta
        return out


class ResBlockGenerator(nn.Module):

    def __init__(self, in_channels, out_channels, dim_embed, bias=True):
        super(ResBlockGenerator, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, out_channels, 3, 1, padding=1, bias=bias)
        self.conv2 = nn.Conv2d(out_channels, out_channels, 3, 1, padding=1, bias=bias)
        nn.init.xavier_uniform_(self.conv1.weight.data, np.sqrt(2))
        nn.init.xavier_uniform_(self.conv2.weight.data, np.sqrt(2))

        # conditional case
        self.condgn1 = ConditionalBatchNorm2d(in_channels, dim_embed)
        self.condgn2 = ConditionalBatchNorm2d(out_channels, dim_embed)
        self.relu = nn.ReLU()
        self.upsample = nn.Upsample(scale_factor=2)

        # unconditional case
        self.model = nn.Sequential(
                nn.BatchNorm2d(in_channels),
                nn.ReLU(),
                nn.Upsample(scale_factor=2),
                self.conv1,
                nn.BatchNorm2d(out_channels),
                nn.ReLU(),
                self.conv2
        )

        self.bypass_conv = nn.Conv2d(in_channels, out_channels, 1, 1, padding=0, bias=bias)  # h=h
        nn.init.xavier_uniform_(self.bypass_conv.weight.data, 1.0)
        self.bypass = nn.Sequential(
                nn.Upsample(scale_factor=2),
                self.bypass_conv,
        )

    def forward(self, x, h):
        # h是连续标签y_cont与离散标签y_class通过嵌入模型得到的特征空间向量
        if h is not None:
            out = self.condgn1(x, h)
            out = self.relu(out)
            out = self.upsample(out)
            out = self.conv1(out)
            out = self.condgn2(out, h)
            out = self.relu(out)
            out = self.conv2(out)
            out = out + self.bypass(x)
        else:
            out = self.model(x) + self.bypass(x)
        return out


class SnganGenerator(nn.Module):
    def __init__(self, nz=256, dim_embed=128, gen_ch=64):
        super(SnganGenerator, self).__init__()
        self.z_dim = nz
        self.dim_embed = dim_embed
        self.gen_ch = gen_ch

        self.dense = nn.Linear(self.z_dim, 4 * 4 * gen_ch * 16, bias=True)
        self.final = nn.Conv2d(gen_ch, channels, 3, stride=1, padding=1, bias=bias)
        nn.init.xavier_uniform_(self.dense.weight.data, 1.)
        nn.init.xavier_uniform_(self.final.weight.data, 1.)

        self.genblock0 = ResBlockGenerator(gen_ch * 16, gen_ch * 8, dim_embed=dim_embed)  # 4--->8
        self.genblock1 = ResBlockGenerator(gen_ch * 8, gen_ch * 4, dim_embed=dim_embed)  # 8--->16
        self.genblock2 = ResBlockGenerator(gen_ch * 4, gen_ch * 2, dim_embed=dim_embed)  # 16--->32
        self.genblock3 = ResBlockGenerator(gen_ch * 2, gen_ch, dim_embed=dim_embed)  # 32--->64

        self.final = nn.Sequential(
                nn.BatchNorm2d(gen_ch),
                nn.ReLU(),
                self.final,
                nn.Tanh()
        )

    def forward(self, z, h):  # h is embedded from y, h is in the feature space
        z = z.view(z.size(0), z.size(1))
        out = self.dense(z)
        out = out.view(-1, self.gen_ch * 16, 4, 4)

        out = self.genblock0(out, h)
        out = self.genblock1(out, h)
        out = self.genblock2(out, h)
        out = self.genblock3(out, h)
        out = self.final(out)

        return out


######################################################################################################################
# Discriminator

class ResBlockDiscriminator(nn.Module):

    def __init__(self, in_channels, out_channels, stride=1):
        super(ResBlockDiscriminator, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, out_channels, 3, 1, padding=1, bias=bias)
        self.conv2 = nn.Conv2d(out_channels, out_channels, 3, 1, padding=1, bias=bias)
        nn.init.xavier_uniform_(self.conv1.weight.data, np.sqrt(2))
        nn.init.xavier_uniform_(self.conv2.weight.data, np.sqrt(2))

        if stride == 1:
            self.model = nn.Sequential(
                    nn.ReLU(),
                    spectral_norm(self.conv1),
                    nn.ReLU(),
                    spectral_norm(self.conv2)
            )
        else:
            self.model = nn.Sequential(
                    nn.ReLU(),
                    spectral_norm(self.conv1),
                    nn.ReLU(),
                    spectral_norm(self.conv2),
                    nn.AvgPool2d(2, stride=stride, padding=0)
            )

        self.bypass_conv = nn.Conv2d(in_channels, out_channels, 1, 1, padding=0, bias=bias)
        nn.init.xavier_uniform_(self.bypass_conv.weight.data, 1.0)
        if stride != 1:
            self.bypass = nn.Sequential(
                    spectral_norm(self.bypass_conv),
                    nn.AvgPool2d(2, stride=stride, padding=0)
            )
        else:
            self.bypass = nn.Sequential(
                    spectral_norm(self.bypass_conv),
            )

    def forward(self, x):
        return self.model(x) + self.bypass(x)


# special ResBlock just for the first layer of the discriminator
class FirstResBlockDiscriminator(nn.Module):

    def __init__(self, in_channels, out_channels, stride=1):
        super(FirstResBlockDiscriminator, self).__init__()

        self.conv1 = nn.Conv2d(in_channels, out_channels, 3, 1, padding=1, bias=bias)
        self.conv2 = nn.Conv2d(out_channels, out_channels, 3, 1, padding=1, bias=bias)
        self.bypass_conv = nn.Conv2d(in_channels, out_channels, 1, 1, padding=0, bias=bias)
        nn.init.xavier_uniform_(self.conv1.weight.data, np.sqrt(2))
        nn.init.xavier_uniform_(self.conv2.weight.data, np.sqrt(2))
        nn.init.xavier_uniform_(self.bypass_conv.weight.data, 1.0)

        # we don't want to apply ReLU activation to raw image before convolution transformation.
        self.model = nn.Sequential(
                spectral_norm(self.conv1),
                nn.ReLU(),
                spectral_norm(self.conv2),
                nn.AvgPool2d(2)
        )
        self.bypass = nn.Sequential(
                nn.AvgPool2d(2),
                spectral_norm(self.bypass_conv),
        )

    def forward(self, x):
        return self.model(x) + self.bypass(x)


class SnganDiscriminator(nn.Module):
    def __init__(self, dim_embed=128, disc_ch=64):
        super(SnganDiscriminator, self).__init__()
        self.dim_embed = dim_embed
        self.disc_ch = disc_ch

        self.discblock1 = nn.Sequential(
                FirstResBlockDiscriminator(channels, disc_ch, stride=2),  # 64--->32
                ResBlockDiscriminator(disc_ch, disc_ch * 2, stride=2),  # 32--->16
                ResBlockDiscriminator(disc_ch * 2, disc_ch * 4, stride=2),  # 16--->8
        )
        self.discblock2 = ResBlockDiscriminator(disc_ch * 4, disc_ch * 8, stride=2)  # 8--->4
        self.discblock3 = nn.Sequential(
                ResBlockDiscriminator(disc_ch * 8, disc_ch * 16, stride=1),  # 4--->4;
                nn.ReLU(),
        )

        self.linear1 = nn.Linear(disc_ch * 16 * 4 * 4, 1, bias=True)
        nn.init.xavier_uniform_(self.linear1.weight.data, 1.)
        self.linear1 = spectral_norm(self.linear1)
        self.linear2 = nn.Linear(self.dim_embed, disc_ch * 16 * 4 * 4, bias=False)
        nn.init.xavier_uniform_(self.linear2.weight.data, 1.)
        self.linear2 = spectral_norm(self.linear2)

    def forward(self, x, h):
        output = self.discblock1(x)
        output = self.discblock2(output)
        output = self.discblock3(output)

        # output = torch.sum(output, dim=(2,3))
        output = output.view(-1, self.disc_ch * 16 * 4 * 4)
        output_h = torch.sum(output * self.linear2(h), 1, keepdim=True)
        output = self.linear1(output) + output_h

        return output.view(-1, 1)


if __name__ == "__main__":
    netG = SnganGenerator(nz=256, dim_embed=128).to(device)
    netD = SnganDiscriminator(dim_embed=128).to(device)

    N = 4
    z = torch.randn(N, 256).to(device)
    y = torch.randn(N, 128).to(device)
    x = netG(z, y)
    o = netD(x, y)
    print(x.size())
    print(o.size())


    def get_parameter_number(net):
        total_num = sum(p.numel() for p in net.parameters())
        trainable_num = sum(p.numel() for p in net.parameters() if p.requires_grad)
        return {'Total': total_num, 'Trainable': trainable_num}


    print('G:', get_parameter_number(netG))
    print('D:', get_parameter_number(netD))
