#written by Jinbae Park
#2019-04

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
import cv2
import tensorflow as tf

# tensorpack
from tensorpack import *
from tensorpack.tfutils.varreplace import remap_variables
from tensorpack.tfutils.summary import add_moving_summary, add_param_summary
from tensorpack.utils import logger

# custom
from .regularization import regularizers
from .optimization.optimizers import get_optimizer
from .activation.activation_funcs import get_activation_func
from .quantization.quantizers import quantize_weight, quantize_activation, quantize_gradient


class Model(ModelDesc):
    def __init__(self, config={}, size=32):
        self.size = size
        self.initializer_config = config['initializer']
        self.regularizer_config = config['regularizer']
        self.optimizer_config = config['optimizer']
        self.activation = get_activation_func(config['activation'])
        self.quantizer_config = config['quantizer']
        
    def inputs(self):
        return [tf.TensorSpec([None, self.size, self.size, 3], tf.float32, 'input'),
                tf.TensorSpec([None], tf.int32, 'label')]

    def build_graph(self, image, label):
        '''
        # get quantization function
        qw = quantize_weight(self.quantizer_config['name'], int(self.quantizer_config['BITW']),
                             eval(self.quantizer_config['midtread']))
        if self.quantizer_config['BITW'] == '32':
            qa = self.activation
        else:
            qa = quantize_activation(int(self.quantizer_config['BITA']))
        qg = quantize_gradient(int(self.quantizer_config['BITG']))
        '''
        from .quantization.dorefa import get_dorefa
        bitW = int(self.quantizer_config['BITW'])
        bitA = int(self.quantizer_config['BITA'])
        bitG = int(self.quantizer_config['BITG'])
        qw, qa, qg = get_dorefa(bitW, bitA, bitG); print(bitW); print(bitA); print(bitG)

        def new_get_variable(v):
            name = v.op.name
            # don't quantize first and last layer
            if not name.endswith('W') or 'conv1' in name or 'fct' in name:
                return v
            else:
                logger.info("Quantizing weight {}".format(v.op.name))
                return qw(v)

        def activate(x):
            return qa(self.activation(x))

        def resblock(x, channel, stride):
            def get_stem_full(x):
                return (LinearWrap(x)
                        .Conv2D('stem_conv_a', channel, 3)
                        .BatchNorm('stem_bn')
                        .apply(activate)
                        .Conv2D('stem_conv_b', channel, 3)())
            
            channel_mismatch = channel != x.get_shape().as_list()[3]
            if stride != 1 or channel_mismatch:
                if stride != 1:
                    x = AvgPooling('avgpool', x, stride, stride)
                x = BatchNorm('bn', x)
                x = activate(x)
                shortcut = Conv2D('shortcut', x, channel, 1)
                stem = get_stem_full(x)
            else:
                shortcut = x
                x = BatchNorm('bn', x)
                x = activate(x)
                stem = get_stem_full(x)
            return shortcut + stem

        def group(x, name, channel, nr_block, stride):
            with tf.variable_scope(name + 'blk1'):
                x = resblock(x, channel, stride)
            for i in range(2, nr_block + 1):
                with tf.variable_scope(name + 'blk{}'.format(i)):
                    x = resblock(x, channel, 1)
            return x

        with remap_variables(new_get_variable), \
                argscope(BatchNorm, decay=0.9, epsilon=1e-4), \
                argscope(Conv2D, use_bias=False, nl=tf.identity,
                         kernel_initializer=tf.variance_scaling_initializer(scale=float(self.initializer_config['scale']),
                                                                            mode=self.initializer_config['mode'])):
            logits = (LinearWrap(image)
                      .Conv2D('conv1', 16, 3)   # size=32
                      .BatchNorm('bn1')
                      .apply(activate)
                      .apply(group, 'res2', 16, 2, 1)  # size=32
                      .apply(group, 'res3', 32, 3, 2)  # size=16
                      .apply(group, 'res4', 64, 3, 2)  # size=8
                      .BatchNorm('last_bn')
                      .apply(activate)
                      .GlobalAvgPooling('gap')
                      .FullyConnected('fct', 10)())
        prob = tf.nn.softmax(logits, name='output')

        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')

        # regularization
        if self.regularizer_config['name'] != 'None':
            reg_func = getattr(regularizers, self.regularizer_config['name'])
            reg_cost = tf.multiply(float(self.regularizer_config['lmbd']), regularize_cost('.*/W', reg_func), name='reg_cost')
            total_cost = tf.add_n([cost, reg_cost], name='total_cost')
        else:
            total_cost = cost

        # summary
        def add_summary(logits, cost):
            err_top1 = tf.cast(tf.logical_not(tf.nn.in_top_k(logits, label, 1)), tf.float32, name='err_top1')
            add_moving_summary(tf.reduce_mean(err_top1, name='train_error_top1'))
            err_top5 = tf.cast(tf.logical_not(tf.nn.in_top_k(logits, label, 5)), tf.float32, name='err_top5')
            add_moving_summary(tf.reduce_mean(err_top5, name='train_error_top5'))

            add_moving_summary(cost)
            add_param_summary(('.*/W', ['histogram']))  # monitor W
        add_summary(logits, cost)
            
        return total_cost

    def optimizer(self):
        opt = get_optimizer(self.optimizer_config)
        return opt

    def get_callbacks(self, ds_tst):
        callbacks=[
            ModelSaver(max_to_keep=1),
            InferenceRunner(ds_tst,
                            [ScalarStats('cross_entropy_loss'),
                             ClassificationError('err_top1', summary_name='validation_error_top1'),
                             ClassificationError('err_top5', summary_name='validation_error_top5')]),
            MinSaver('validation_error_top1'),
            ScheduledHyperParamSetter('learning_rate',
                                      [(1, 0.1), (82, 0.01), (123, 0.001), (300, 0.0002)])
        ]
        max_epoch = 200
        return callbacks, max_epoch
