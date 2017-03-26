"""Sequence-to-sequence models with dynamic unrolling and faster embedding techniques."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import logging
import numpy as np
import tensorflow as tf
from chatbot import bot_ops
from chatbot._models import Model, OPTIMIZERS
from chatbot.recurrent_components import Encoder, Decoder
from chatbot.input_components import InputPipeline, Embedder
from utils import io_utils


class DynamicBot(Model):

    def __init__(self, dataset, model_params):

        logging.basicConfig(level=logging.INFO)
        self.log = logging.getLogger('DynamicBotLogger')
        # Let superclass handle the boring stuff (dirs/more instance variables).
        super(DynamicBot, self).__init__(self.log,
                                         model_params)

        with tf.variable_scope("input_layer"):
            self.pipeline       = InputPipeline(dataset.paths,
                                                self.params['batch_size'],
                                                is_chatting=self.params['is_chatting'])
            self.encoder_inputs = self.pipeline.encoder_inputs
            self.decoder_inputs = self.pipeline.decoder_inputs

        self.embedder = Embedder(self.vocab_size, embed_size, l1_reg=l1_reg)
        with tf.variable_scope("encoder") as scope:
            embedded_enc_inputs = self.embedder(self.encoder_inputs, scope=scope)
            # Create the encoder & decoder objects.
            self.encoder  = Encoder(state_size, self.embed_size,
                                    dropout_prob=dropout_prob,
                                    num_layers=num_layers)
            # Applying embedded inputs to encoder yields the final (context) state.
            encoder_state = self.encoder(embedded_enc_inputs)

        with tf.variable_scope("decoder") as scope:
            embedded_dec_inputs = self.embedder(self.decoder_inputs, scope=scope)
            self.decoder  = Decoder(state_size, self.vocab_size, self.embed_size,
                                    dropout_prob=dropout_prob,
                                    num_layers=num_layers,
                                    max_seq_len=dataset.max_seq_len,
                                    temperature=temperature)
            # For decoder, we want the full sequence of output states, not simply the last.
            decoder_outputs, decoder_state = self.decoder(embedded_dec_inputs,
                                                          initial_state=encoder_state,
                                                          is_chatting=is_chatting,
                                                          loop_embedder=self.embedder,
                                                          scope=scope)

        # Merge any summaries floating around in the aether into one object.
        self.merged = tf.summary.merge_all()
        self.outputs = decoder_outputs


    def compile(self, optimizer=None, max_gradient=5.0, reset=False, sampled_loss=False):
        """ Configure training process and initialize model. Inspired by Keras.

        Args:
            optimizer: (str). Supported: 'Adagrad', 'RMSProp', 'SGD', 'Adam'.
            max_gradient: float. Gradients will be clipped to be below this value.
            reset: boolean. Tells Model superclass whether or not we wish to compile
                            a model from scratch or load existing parameters from ckpt_dir.
            sampled_loss: (bool) gives user the option to toggle sampled_loss
                          on/off post-initialization.
        """

        if not self.is_chatting:
            with tf.variable_scope("evaluation") as scope:
                # Loss - target is to predict, as output, the next decoder input.
                # target_labels has shape [batch_size, dec_inp_seq_len - 1]
                target_labels = self.decoder_inputs[:, 1:]
                target_weights = tf.cast(target_labels > 0, target_labels.dtype)
                preds = self.decoder.apply_projection(self.outputs)
                regLosses = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
                l1 = tf.reduce_sum(tf.abs(regLosses))
                if sampled_loss:
                    self.log.info("Training with dynamic sampled softmax loss.")
                    assert 0 < self.num_samples < self.vocab_size, \
                        "num_samples is %d but should be between 0 and %d" \
                        % (self.num_samples, self.vocab_size)
                    self.loss = bot_ops.dynamic_sampled_softmax_loss(
                        target_labels, self.outputs[:, :-1, :],
                        self.decoder.get_projection_tensors(), self.vocab_size,
                        num_samples=self.num_samples) + l1
                else:
                    self.loss = tf.losses.sparse_softmax_cross_entropy(
                        labels=target_labels, logits=preds[:, :-1, :],
                        weights=target_weights) + l1

                # Define the training portion of the graph.
                if optimizer is not None:
                    assert optimizer in OPTIMIZERS, \
                        "Optimizer %s not supported. Choice are:\n%r" \
                    % (optimizer, OPTIMIZERS.keys())
                else:
                    optimizer = 'Adam'

                self.log.info("Optimizing with %s." % optimizer)
                self.apply_gradients = tf.contrib.layers.optimize_loss(
                    loss=self.loss, global_step=self.global_step,
                    learning_rate=self.learning_rate,
                    optimizer=optimizer,
                    clip_gradients=max_gradient,
                    summaries=['loss', 'gradients'])

                # Compute accuracy, ensuring we use fully projected outputs.
                correct_pred = tf.equal(tf.argmax(preds[:, :-1, :], axis=2), target_labels)
                accuracy = tf.reduce_mean(tf.cast(correct_pred, tf.float32))
                tf.summary.scalar('accuracy', accuracy)
                self.merged = tf.summary.merge_all()

        # Let superclass load param values from file (if reset==False), else initialize new model.
        super(DynamicBot, self).compile(reset=reset)

    def step(self, forward_only=False):
        """Run one step of the model, which can mean 1 of the following:
            1. forward_only == False. This means we are training, so we should do both a
               forward and a backward pass.
            2. self.is_chatting. When chatting, we just get the response (word ID sequence).
            3. default to inference. Do a forward pass, but also compute loss(es) and summaries.

        Args:
            forward_only: if True, don't perform backward pass (gradient updates).

        Returns:
            summaries, step_loss, step_outputs.
            If forward_only == False, then outputs is None
        """

        if not forward_only:
            fetches = [self.merged, self.loss, self.apply_gradients]
            summaries, step_loss, _ = self.sess.run(fetches)
            return summaries, step_loss, None
        elif self.is_chatting:
            response = self.sess.run(self.outputs, feed_dict=self.pipeline.feed_dict)
            return None, None, response
        else:
            fetches = [self.merged, self.loss] # , self.outputs]
            summaries, step_loss = self.sess.run(fetches)
            return summaries, step_loss, None

    def train(self, dataset):
        """Train bot on inputs until user types CTRL-C or queues run out of data.

        Args:
            dataset: any instance of the Dataset class.
        """

        def perplexity(loss): return np.exp(float(loss)) if loss < 300 else float("inf")

        coord   = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=self.sess, coord=coord)

        # Tell embedder to coordinate with TensorBoard's embedding visualization.
        # This allows to view, e.g., our words in 3D-projected embedding space (with labels!).
        label_paths = [dataset.paths['from_vocab'], dataset.paths['to_vocab']]
        self.embedder.assign_visualizer(self.file_writer, 'encoder', label_paths[0])
        self.embedder.assign_visualizer(self.file_writer, 'decoder', label_paths[1])

        # Note: Calling sleep(...) appears to allow sustained GPU utilization across training.
        # Without it, looks like GPU has to wait for data to be enqueued more often. Strange.
        print('QUEUE RUNNERS RELEASED.'); time.sleep(4)
        print('GO!')

        try:
            i_step = 0
            avg_loss = avg_step_time = 0.0
            while not coord.should_stop():

                start_time = time.time()
                summaries, step_loss, _ = self.step()
                # Calculate running averages.
                avg_step_time  += (time.time() - start_time) / self.steps_per_ckpt
                avg_loss       += step_loss / self.steps_per_ckpt

                # Print updates in desired intervals (steps_per_ckpt).
                if i_step % self.steps_per_ckpt == 0:
                    # Display averged-training updates and save.
                    print("Step %d:" % i_step, end=" ")
                    print("step time = %.3f" % avg_step_time)
                    print("\ttraining loss = %.3f" % avg_loss, end="; ")
                    print("training perplexity = %.2f" % perplexity(avg_loss))

                    # Toggle data switch and led the validation flow!
                    self.pipeline.toggle_active()
                    with self.graph.device('/cpu:0'):
                        summaries, eval_loss, _ = self.step(forward_only=True)
                        self.save(summaries=summaries)
                    self.pipeline.toggle_active()
                    print("\tValidation loss = %.3f" % eval_loss, end="; ")
                    print("val perplexity = %.2f" % perplexity(eval_loss))
                    # Reset the running averages and exit checkpoint.
                    avg_loss = avg_step_time = 0.0

                i_step += 1
        except (KeyboardInterrupt, SystemExit):
            print("Training halted. Cleaning up . . . ")
            coord.request_stop()
        except tf.errors.OutOfRangeError:
            print("OutOfRangeError. You have run out of data. Get some more.")
            coord.request_stop()
        finally:
            coord.join(threads)
            self.close()

    def decode(self):
        """
        The higher the temperature, the more varied will be the bot's responses.
        """
        # We decode one sentence at a time.
        self.batch_size = 1
        assert self.is_chatting
        # Decode from standard input.
        print("Type \"exit\" to exit.")
        print("Hi human. Write stuff below and I, your robot friend, will respond.")
        sentence = io_utils.get_sentence()
        while sentence:
            response = self(sentence)
            print("Robot:", response)
            sentence = io_utils.get_sentence()
            if sentence == 'exit':
                print("Farewell, human.")
                break

    def __call__(self, sentence):
        """This is how we talk to the bot."""
        # Convert input sentence to token-ids.
        encoder_inputs = io_utils.sentence_to_token_ids(
            tf.compat.as_bytes(sentence),self.dataset.word_to_idx)

        encoder_inputs = np.array([encoder_inputs[::-1]])
        self.pipeline.feed_user_input(encoder_inputs)
        # Get output sentence from the chatbot.
        _, _, response = self.step(forward_only=True)
        # response has shape [1, response_length] and it's last elemeot is EOS_ID. :)
        return self.dataset.as_words(response[0][:-1])
