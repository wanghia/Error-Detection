import time
import codecs
import numpy as np
from numpy import *
import tensorflow as tf
from my import reader
#import reader
from my import util
import os
from tensorflow.python.client import device_lib
import predict_result

flags = tf.flags
logging = tf.logging

flags.DEFINE_string("model", "train",
    "A type of model. Possible options are: train, test.")
flags.DEFINE_string("data_path", "./corpus/",
                    "Where the training/test data is stored.")
flags.DEFINE_string("save_path", "./model_proba/proba_total_bi/",
                    "Model output directory.")
flags.DEFINE_string("test_path", "./test/new_test",
                    "Model test file.")
flags.DEFINE_bool("use_fp16", False,
                  "Train using 16-bit floats instead of 32bit floats")
flags.DEFINE_integer("num_gpus", 1,
                     "If larger than 1, Grappler AutoParallel optimizer "
                     "will create multiple training replicas with each GPU "
                     "running one replica.")
flags.DEFINE_bool("pretrained_embedding", False, "Determing whether to use pre-trained embedding or not")
flags.DEFINE_string("rnn_mode", "BLOCK",
                    "The low level implementation of lstm cell: one of CUDNN, "
                    "BASIC, and BLOCK, representing cudnn_lstm, basic_lstm, "
                    "and lstm_block_cell classes.")
FLAGS = flags.FLAGS
BASIC = "basic"
CUDNN = "cudnn"
BLOCK = "block"

embedding = None

def data_type():
  return tf.float16 if FLAGS.use_fp16 else tf.float32

class PTBInput(object):
  """The input data."""

  def __init__(self, config, data, seq_length, name=None, is_training = True):
    self.batch_size = batch_size = config.batch_size
    self.num_steps = num_steps = config.num_steps
    self.epoch_size = ((len(data) // batch_size) - 1) // num_steps
    self.input_data, self.targets, self.seq_length = reader.ptb_producer(
        data,seq_length, batch_size, num_steps, name=name, is_training = is_training, test_path = FLAGS.test_path)
    self.seq_length = tf.reshape(self.seq_length,[-1])


class PTBModel(object):
  """The PTB model."""

  def __init__(self, is_training, config, input_):
    self._is_training = is_training
    self._input = input_
    self._rnn_params = None
    self._cell = None
    self.batch_size = input_.batch_size
    self.num_steps = input_.num_steps
    self.vocab_size = len(reader.word_to_id)+1
    self.embedding_size = config.embedding_size

    # Embedding part : Can use pre-trained embedding.
    with tf.device("/cpu:0"):
      if FLAGS.pretrained_embedding == False:
       self.embedding = tf.get_variable(name = "embedding", shape = [self.vocab_size, self.embedding_size], initializer = tf.truncated_normal_initializer, dtype=tf.float32)
      else:
        if os.path.exists("./embedding.txt"):
          self.loadEmbedding()
        else:
          self.usePreEmbedding("../Keras/w2c_financial.txt")
        self.embedding = tf.get_variable(name = "embedding", initializer=tf.convert_to_tensor(self.embedding), dtype=tf.float32)
      inputs = tf.nn.embedding_lookup(self.embedding, input_.input_data)

    # get predict word's distribution
    output, state = self._build_rnn_graph(inputs, config, is_training)
    
    self._final_state_fw = state[0]
    self._final_state_bw = state[1]

    output = tf.contrib.layers.flatten(output)
    #logits = tf.contrib.layers.fully_connected(output, 20, activation_fn=tf.nn.tanh)
    #xx = layers.batch_norm(xx)
    #xx = tf.nn.relu(xx)
    logits = tf.contrib.layers.fully_connected(output, 2, activation_fn=None)
    # Reshape logits to be a 3-D tensor for sequence loss
    logits = tf.reshape(logits, [self.batch_size, self.num_steps, 2])
    
    label_reshape = tf.cast(tf.reshape(input_.targets, [self.batch_size, self.num_steps,2]), tf.float32)
    
    loss_mask = tf.sequence_mask(tf.to_int32(input_.seq_length), tf.to_int32(tf.shape(label_reshape)[1]))
    class_weight = tf.constant([1.0, 0.07])
    weight_per_label = tf.transpose( tf.matmul(tf.reshape(label_reshape,[-1,2]), tf.transpose(tf.reshape(class_weight,[1,2]))) ) #shape [1,num_steps*batch_size]
    loss = tf.multiply(tf.reshape(weight_per_label,[self.batch_size,self.num_steps]), tf.nn.softmax_cross_entropy_with_logits(logits = logits, labels=label_reshape))
    loss = tf.boolean_mask(loss ,loss_mask ) 
    self._cost = tf.reduce_sum(loss)
    loss_mask = tf.cast(loss_mask, tf.int32)
    self.logits = tf.reshape(tf.cast(tf.argmax(logits, axis=2), tf.int32),[-1, self.num_steps]) 
    label_reshape = tf.cast(tf.argmax(label_reshape, axis=2), tf.int32)
    label_reshape = tf.multiply(label_reshape , loss_mask)
    masked_logits = tf.multiply(self.logits , loss_mask)
    correct_prediction = tf.cast(tf.equal( masked_logits, label_reshape ), tf.float32)

    correct_prediction = tf.reduce_min(correct_prediction, axis=1)
    self.accuracy = tf.reduce_mean(correct_prediction)
	  
    if not is_training:
      return

    self._lr = tf.Variable(0.0, trainable=False)
    tvars = tf.trainable_variables()
    grads, _ = tf.clip_by_global_norm(tf.gradients(self._cost, tvars),config.max_grad_norm)
    optimizer = tf.train.AdamOptimizer(self._lr)
    self._train_op = optimizer.apply_gradients(zip(grads, tvars),global_step=tf.train.get_or_create_global_step())

    self._new_lr = tf.placeholder(tf.float32, shape=[], name="new_learning_rate")
    self._lr_update = tf.assign(self._lr, self._new_lr)

  def resetInput(self, input_):
    self._input = input_

  def _build_rnn_graph(self, inputs, config, is_training):
    return self._build_rnn_graph_lstm(inputs, config, is_training)

  def _get_lstm_cell(self, config, is_training):
    if config.rnn_mode == BASIC:
      return tf.contrib.rnn.BasicLSTMCell(
          config.hidden_size, forget_bias=0.0, state_is_tuple=True,
          reuse= not is_training)
    if config.rnn_mode == BLOCK:
      return tf.contrib.rnn.LSTMBlockCell(
          config.hidden_size, forget_bias=0.0)
    raise ValueError("rnn_mode %s not supported" % config.rnn_mode)

  def _build_rnn_graph_lstm(self, inputs, config, is_training):
    def make_cell():
      cell = self._get_lstm_cell(config, is_training)
      if is_training and config.keep_prob < 1:
        cell = tf.contrib.rnn.DropoutWrapper(
            cell, output_keep_prob=config.keep_prob)
      return cell
    cell_fw = tf.contrib.rnn.MultiRNNCell([make_cell() for _ in range(config.num_layers)], state_is_tuple=True)
    cell_bw = tf.contrib.rnn.MultiRNNCell([make_cell() for _ in range(config.num_layers)], state_is_tuple=True)

    self._initial_state_fw = cell_fw.zero_state(config.batch_size, data_type())
    self._initial_state_bw = cell_bw.zero_state(config.batch_size, data_type())

    #outputs, state = tf.nn.dynamic_rnn(cell, inputs, sequence_length = self._input.seq_length , initial_state=self._initial_state)
    outputs, state = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, inputs, sequence_length = self._input.seq_length , initial_state_fw=self._initial_state_fw , initial_state_bw=self._initial_state_bw )
    outputs = tf.concat(outputs, 2)
    output = tf.reshape(outputs, [-1, config.hidden_size*2])
    return output, state

  def usePreEmbedding(self, embeddingf ,save = True):
    global embedding
    # use pre-trained embedding
    print("Using Pre-trained Embedding...")
    embedding_dict = {}
    f = codecs.open(embeddingf, "r", encoding="utf8",errors='ignore')
    for line in f:
      values = line.split()
      word = values[0]
      coefs = asarray(values[1:],dtype='float32')
      embedding_dict[word] = coefs
    f.close()
    embedding = zeros( (vocab_size, 300), dtype=float32)
    for word, i in reader.word_to_id.items():
      embedding_vector = embedding_dict.get(word)
      if embedding_vector is  None:
        embedding_vector = zeros((1,300), dtype=float32)
        for character in word:
          if character in embedding_dict:
            embedding_vector = embedding_vector + embedding_dict.get(character)
        embedding_vector = embedding_vector / len(word)
      embedding[i] = embedding_vector
    if save == True:
      saveEmebdding()
    return embedding

  def saveEmebdding(self):
    global embedding
    print("Saving Embedding...")
    ff = open("./embedding.txt","wb")
    for i in embedding:
      savetxt(ff,i,fmt="%f")
    ff.close()
    print("Saving Embedding Finish")

  def loadEmbedding(self):
    global embedding
    print("Loading Embedding...")
    embedding = []
    embedding  = loadtxt("./embedding.txt", dtype = float32)
    embedding = reshape(embedding,[-1,300])
    vocab_size = embedding.shape[0]
    print("Loading Embedding Finish")
    return embedding

  def assign_lr(self, session, lr_value):
    session.run(self._lr_update, feed_dict={self._new_lr: lr_value})

  @property
  def input(self):
    return self._input

  @property
  def initial_state_fw(self):
    return self._initial_state_fw

  @property
  def initial_state_bw(self):
    return self._initial_state_bw

  @property
  def output(self):
    return self.logits

  @property
  def cost(self):
    return self._cost

  @property
  def final_state_fw(self):
    return self._final_state_fw

  @property
  def final_state_bw(self):
    return self._final_state_bw

  @property
  def lr(self):
    return self._lr

  @property
  def train_op(self):
    return self._train_op

  @property
  def initial_state_fw_name(self):
    return self._initial_state_fw_name

  @property
  def initial_state_bw_name(self):
    return self._initial_state_bw_name
  
  @property
  def final_state_fw_name(self):
    return self._final_state_fw_name

  @property
  def final_state_bw_name(self):
    return self._final_state_bw_name


class MediumConfig(object):
  """Medium config."""
  batch_size = 128
  max_grad_norm = 3
  learning_rate = 0.0001

  keep_prob = 0.8
  lr_decay = 0.98
  init_scale = 0.05
  hidden_size = 400
  embedding_size = 400

  max_epoch = 8
  max_max_epoch = 1
  max_max_max_epoch = 100

  num_layers = 1
  num_steps = 47
  vocab_size = 9174
  rnn_mode = BLOCK

  def __str__(self):
    return ("batch_size: {}, learning_rate: {}, keep_prob: {}, max_grad_norm: {}, init_scale: {}, hidden_size: {}, embedding_size: {}, num_layers: {}".format(self.batch_size,self.learning_rate,self.keep_prob,self.max_grad_norm,self.init_scale,self.hidden_size,self.embedding_size,self.num_layers))

def run_epoch(session, model, eval_op=None, verbose=False, is_training=True, save_file=None):
  
  """Runs the model on the given data."""
  start_time = time.time()
  costs = 0.0
  iters = 0
  acc = 0.0
  state_fw,state_bw = session.run([model.initial_state_fw,model.initial_state_bw])
  fetches = {
      "cost": model.cost,
      "final_state_fw": model.final_state_fw,
      "final_state_bw": model.final_state_bw,
      "accuracy": model.accuracy,
      "logits": model.logits,
      #"targets":model.targets,
      #"length":model.length
      #"loss_masked": model.loss_masked
  }
  if eval_op is not None:
    fetches["eval_op"] = eval_op
  for step in range(model.input.epoch_size):
    feed_dict = {}
    for i, (c, h) in enumerate(model.initial_state_fw):
      feed_dict[c] = state_fw[i].c
      feed_dict[h] = state_fw[i].h

    for i, (c, h) in enumerate(model.initial_state_bw):
      feed_dict[c] = state_bw[i].c
      feed_dict[h] = state_bw[i].h

    vals = session.run(fetches, feed_dict )
    if is_training==False:
      if save_file is not None:
        result = vals["logits"]
        #print(result)
        #for word in (result):
        #  save_file.write(str(word)+" ")
        #save_file.write("\n")
        predict_result.genPredict(array(result,dtype=int32), test_path = FLAGS.test_path)
      
    cost = vals["cost"]
    state_fw = vals["final_state_fw"]
    state_bw = vals["final_state_bw"]
    costs += cost
    iters += model.input.num_steps
    acc += vals["accuracy"]
    if verbose and step % (model.input.epoch_size // 10) == 10:
      print("%.3f Loss: %.3f Speed: %.0f wps Acc: %.3f" %(
          step * 1.0 / model.input.epoch_size, 
          np.exp(costs / iters),
          iters * model.input.batch_size * max(1, FLAGS.num_gpus) / (time.time() - start_time),
          acc / (iters//model.input.num_steps)
        )
      )

  return np.exp(costs / iters), acc / (iters//model.input.num_steps)


def get_config():
  """Get model config."""
  temconfig = MediumConfig()
  mode = 0
  if FLAGS.model == "test":
    mode = 1
  if FLAGS.rnn_mode:
    temconfig.rnn_mode = FLAGS.rnn_mode
  if FLAGS.num_gpus != 1 or tf.__version__ < "1.3.0" :
    temconfig.rnn_mode = BASIC
  return temconfig, mode


def main(_):
  if not FLAGS.data_path:
    raise ValueError("Must set --data_path to PTB data directory")
  gpus = [
      x.name for x in device_lib.list_local_devices() if x.device_type == "GPU"
  ]
  if FLAGS.num_gpus > len(gpus):
    raise ValueError(
        "Your machine has only %d gpus "
        "which is less than the requested --num_gpus=%d."
        % (len(gpus), FLAGS.num_gpus))

  config, mode = get_config()
  eval_config,_ = get_config()
  dev_config,_ = get_config()
  eval_config.keep_prob = 1
  eval_config.batch_size = 1
  dev_config.keep_prob = 1

  if mode == 0:
    # train mode
    print("Enter Train Mode:")
    train_data,train_seq_length, dev_data, dev_seq_length = reader.ptb_raw_data(FLAGS.data_path, is_training = True, index = 0)
    test_data,test_seq_length = reader.ptb_raw_data(FLAGS.test_path, is_training = False)
    with tf.Graph().as_default():
      initializer = tf.random_uniform_initializer(-config.init_scale,config.init_scale)
      with tf.name_scope("Train"):
        train_input = PTBInput(config=config, data=train_data, seq_length= train_seq_length, name="TrainInput")
        with tf.variable_scope("Model", reuse=None , initializer=initializer) as scope:
          m = PTBModel(is_training=True, config=config, input_=train_input)
          scope.reuse_variables()
        tf.summary.scalar("Training Loss", m.cost)
        tf.summary.scalar("Learning Rate", m.lr)
      
      with tf.name_scope("Dev"):
        dev_input = PTBInput(config=dev_config, data=dev_data, seq_length = dev_seq_length, name="DevInput")
        with tf.variable_scope("Model", reuse=True , initializer=initializer) as scope:
          devm = PTBModel(is_training=False, config=dev_config, input_=dev_input)

      with tf.name_scope("Test"):
        test_input = PTBInput(config=eval_config, data=test_data, seq_length = test_seq_length, name="TestInput", is_training = False)
        with tf.variable_scope("Model", reuse=True , initializer=initializer) as scope:
          testm = PTBModel(is_training=False, config=eval_config, input_=test_input)
      
      sv = tf.train.Supervisor(logdir=FLAGS.save_path)
      config_proto = tf.ConfigProto(allow_soft_placement=True)
      with sv.managed_session(config=config_proto) as session:
        for total_epoch in range(config.max_max_max_epoch):
          for train_round in range(84):
            print("="*20)
            print("Now Training index: %d"%train_round)

            tf.reset_default_graph()
            train_data,train_seq_length, dev_data, dev_seq_length = reader.ptb_raw_data(FLAGS.data_path, is_training = True, index = train_round)
            train_input = PTBInput(config=config, data=train_data, seq_length= train_seq_length, name="TrainInput")
            dev_input = PTBInput(config=eval_config, data=dev_data, seq_length= dev_seq_length, name="DevInput")
            m.resetInput(train_input)
            devm.resetInput(dev_input)
            for i in range(config.max_max_epoch):
              
              print("TrainTrain")
              lr_decay = config.lr_decay ** max(i + 1 - config.max_epoch, 0.0)
              m.assign_lr(session, config.learning_rate * lr_decay)
              print("Epoch: %d Learning rate: %.3f" % (i + 1, session.run(m.lr)))
              train_perplexity,acc = run_epoch(session, m, eval_op=m.train_op,verbose=True)
              print("Epoch: %d Train Loss: %.3f Acc: %.3f" % (i + 1, train_perplexity,acc))
              
              dev_perplexity ,acc= run_epoch(session, devm, is_training = False)
              print("Epoch: %d Dev Loss: %.3f Acc: %.3f" % (i + 1, dev_perplexity, acc))
              
              # test
              length = reader.length
              print(length)
              #save_file = open("./result_proba_"+str(train_round)+".txt","w")
              _,acc = run_epoch(session,testm, is_training = False, save_file = "test")
              test_acc, f1score = predict_result.savePredict(train_round, test_path = FLAGS.test_path, config= config, describ = FLAGS.save_path)
              print("Epoch: %d Test Acc: %.3f Evaluate Acc: %.3f F1: %.3f" % (i + 1,acc, test_acc, f1score))
              #save_file.close()

              if os.path.exists(FLAGS.save_path):
                print("Saving model to %s." % FLAGS.save_path)
                sv.saver.save(session, FLAGS.save_path+"model.ckpt", global_step=sv.global_step)

  else:
    print("Enter Test Mode:")
    test_data,test_seq_length = reader.ptb_raw_data(FLAGS.test_path, is_training = False)
    length = reader.length
    config.keep_prob = 1
    config.batch_size = 1
    with tf.Graph().as_default():
      initializer = tf.random_uniform_initializer(-eval_config.init_scale,
                                                  eval_config.init_scale)
      with tf.name_scope("Train"):
        test_input = PTBInput(config=eval_config, data=test_data, seq_length = test_seq_length, name="TrainInput")
        with tf.variable_scope("Model", reuse=None, initializer=initializer):
          m = PTBModel(is_training=True, config=eval_config, input_=test_input)
          
      sv = tf.train.Supervisor(logdir=FLAGS.save_path)
      config_proto = tf.ConfigProto(allow_soft_placement=True)

      with sv.managed_session(config=config_proto) as session:
        ckpt = tf.train.get_checkpoint_state(checkpoint_dir=FLAGS.save_path)
        sv.saver.restore(session,ckpt.model_checkpoint_path)

        length = reader.length
        save_file = open("./result_proba_"+str(train_round)+".txt","w")
        print(length)
        for sublength in range(length):
          run_epoch(session,m, is_training = False)
        save_file.close()
        #predict_result.saveResult(-1, test_path = FLAGS.test_path)

if __name__ == "__main__":
  tf.app.run()










"""
class LargeConfig(object):
  #Large config.
  init_scale = 0.04
  learning_rate = 1.0
  max_grad_norm = 10
  num_layers = 2
  num_steps = 35
  hidden_size = 1500
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 1 / 1.15
  batch_size = 20
  vocab_size = 10000
  rnn_mode = BLOCK


class TestConfig(object):
  #Tiny config, for testing.
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 1
  num_layers = 1
  num_steps = 2
  hidden_size = 2
  max_epoch = 1
  max_max_epoch = 1
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000
  rnn_mode = BLOCK

class SmallConfig(object):
  #Small config.
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 2
  num_steps = 20
  hidden_size = 200
  max_epoch = 4
  max_max_epoch = 13
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000
  rnn_mode = BLOCK
"""