import math

import tensorflow as tf

import tensorflow.contrib.slim as slim

from tensorflow.contrib.rnn.python.ops import core_rnn_cell
from tensorflow.contrib.legacy_seq2seq.python.ops import seq2seq as seq2seq_lib

from yt8m.models import models
import yt8m.models.model_utils as utils
import yt8m.starter.video_level_models as video_level_models
import attn


class SkipThought(models.BaseModel):
  def __init__(self):
    super(SkipThought, self).__init__()
    self.normalize_input = False
    self.clip_global_norm = 5
    self.var_moving_average_decay = 0.9997
    self.optimizer_name = "AdamOptimizer"
    self.base_learning_rate = 4e-3

    self.cell_size = 1024

  def create_model(self, model_input, vocab_size, num_frames,
                   is_training=True, dense_labels=None, feature_sizes=None,
                   input_weights=None,
                   **unused_params):
    self.is_training = is_training
    feature_size = sum(feature_sizes)
    num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
    # TODO
    self.max_steps = 300 # 30
    enc_inputs = utils.SampleRandomSequence(model_input, num_frames,
                                            self.max_steps)

    enc_cell = self.get_enc_cell(self.cell_size, self.cell_size)
    dec_cell = self.get_dec_cell(self.cell_size)
    runtime_batch_size = tf.shape(enc_inputs)[0]

    enc_init_state = enc_cell.zero_state(runtime_batch_size, dtype=tf.float32)
    enc_outputs, enc_state = tf.nn.dynamic_rnn(
        enc_cell, enc_inputs, initial_state=enc_init_state, scope="enc")

    if True:
      enc_outputs_stopped = tf.stop_gradient(enc_outputs)
      input_weights = tf.tile(
          tf.expand_dims(input_weights, 2),
          [1, 1, self.cell_size])
      enc_outputs_stopped = enc_outputs_stopped * input_weights
      enc_rep = tf.reduce_sum(enc_outputs_stopped, axis=1) / num_frames
      # enc_rep = tf.reduce_sum(enc_outputs_stopped, axis=1) / self.max_steps

      self.vocab_size = vocab_size
      cls_func = self.moe
      logits = cls_func(enc_rep)

      if cls_func == self.moe:
        epsilon = 1e-12
        labels = tf.cast(dense_labels, tf.float32)
        cross_entropy_loss = labels * tf.log(logits + epsilon) + (
            1 - labels) * tf.log(1 - logits + epsilon)
        cross_entropy_loss = tf.negative(cross_entropy_loss)
        loss = tf.reduce_mean(tf.reduce_sum(cross_entropy_loss, 1))

        predictions = logits
      else:
        loss = tf.nn.sigmoid_cross_entropy_with_logits(labels=tf.cast(dense_labels, tf.float32),
                                                       logits=logits)
        loss = tf.reduce_mean(tf.reduce_sum(loss, 1))
        predictions = tf.nn.sigmoid(logits)
    else:
      dec_targets = tf.unstack(enc_inputs, axis=1)
      dec_targets.reverse()
      dec_inputs = [tf.zeros_like(dec_targets[0])] + dec_targets[:-1]

      dec_outputs, _ = attn.attention_decoder(decoder_inputs=dec_inputs,
                                              initial_state=enc_state,
                                              attention_states=enc_outputs,
                                              cell=dec_cell,
                                              output_size=feature_size,
                                              dtype=tf.float32)
      dec_weights = []
      for _ in xrange(self.max_steps):
        dec_weights.append(tf.ones([runtime_batch_size, ], dtype=tf.float32))
      loss = seq2seq_lib.sequence_loss(
          dec_outputs, dec_targets, dec_weights,
          softmax_loss_function=self.reconstruct_loss)
      predictions = tf.no_op()
    return {
        "loss": loss,
        "predictions": predictions,
    }

  def one_fc(self, enc_rep):
    logits = slim.fully_connected(
        enc_rep, self.vocab_size, activation_fn=None,
        weights_regularizer=slim.l2_regularizer(1e-8))
    return logits

  def three_fc(self, enc_rep):
    logits = slim.fully_connected(
        enc_rep, 4096, activation_fn=tf.nn.relu,
        weights_regularizer=slim.l2_regularizer(1e-8),
        scope="OutputFC0")
    if is_training:
      logits = tf.nn.dropout(logits, 0.8)
    logits = slim.fully_connected(
        logits, 4096, activation_fn=tf.nn.relu,
        weights_regularizer=slim.l2_regularizer(1e-8),
        scope="OutputFC1")
    if is_training:
      logits = tf.nn.dropout(logits, 0.8)
    logits = slim.fully_connected(
        logits, self.vocab_size, activation_fn=None,
        weights_regularizer=slim.l2_regularizer(1e-8),
        scope="OutputFC2")
    return logits

  def moe(self, enc_rep):
     moe = video_level_models.MoeModel()
     res = moe.create_model(
         enc_rep, self.vocab_size,
         num_mixtures=10)
     return res["predictions"]


  def get_variables_with_ckpt(self):
    exclusions = "OutputFC"
    if self.is_training:
      variable_to_restore = []
      for var in tf.trainable_variables():
        excluded = False
        for exclusion in exclusions:
          if var.op.name.startswith(exclusion):
            excluded = True
            break
        if not excluded:
          variable_to_restore.append(var)
          print(var.op.name)
      return variable_to_restore
    else:
      return tf.all_variables()

  def get_enc_cell(self, cell_size, vocab_size):
    cell = core_rnn_cell.GRUCell(cell_size)
    # TODO
    # if self.is_training:
      # cell = core_rnn_cell.DropoutWrapper(cell, 0.5, 0.5)
    cell = core_rnn_cell.InputProjectionWrapper(cell, cell_size)
    cell = core_rnn_cell.OutputProjectionWrapper(cell, cell_size)
    return cell

  def get_dec_cell(self, cell_size):
    cell = core_rnn_cell.GRUCell(cell_size)
    cell = core_rnn_cell.DropoutWrapper(cell, 0.5, 0.5)
    # num_layers = 1
    # cell = core_rnn_cell.MultiRNNCell([cell] * num_layers)
    return cell

  def reconstruct_loss(self, logit, target):
    # Huber loss
    sigma = 2.
    delta = sigma * sigma
    d = logit - target
    if True:
      a = .5 * delta * d * d
      b = tf.abs(d) - 0.5 / delta
      l = tf.where(tf.abs(d) < (1. / delta), a, b)
    else:
      l = .5 * d * d
    # loss = tf.reduce_sum(d * d, reduction_indices=1)
    loss = tf.reduce_sum(l, reduction_indices=1)
    return loss
