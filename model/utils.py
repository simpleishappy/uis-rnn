# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utils for UIS-RNN."""

import numpy as np
import torch
from torch import autograd


def weighted_mse_loss(input_tensor, target_tensor, weight=1):
  """Compute weighted mse loss.

  Note that we are doing weighted loss that only sum up over non-zero entries.

  Args:
    input_tensor: input tensor
    target_tensor: target tensor
    weight: weight tensor, in this case 1/sigma^2

  Returns:
    weighted mse loss
  """
  observation_dim = input_tensor.size()[-1]
  streched_tensor = ((input_tensor - target_tensor) ** 2).view(
      -1, observation_dim)
  entry_num = float(streched_tensor.size()[0])
  non_zero_entry_num = torch.sum(streched_tensor[:, 0] != 0).float()
  weighted_tensor = torch.mm(
      ((input_tensor - target_tensor)**2).view(-1, observation_dim),
      (torch.diag(weight.float().view(-1))))
  return torch.mean(
      weighted_tensor) * weight.nelement() * entry_num / non_zero_entry_num


def sample_permuted_segments(index_sequence, number_samples):
  """Sample sequences with permuted blocks.

  Args:
    index_sequence: (integer array, size: L)
      - subsequence index
      For example, index_sequence = [1,2,6,10,11,12].
    number_samples: (integer)
      - number of subsampled block-preserving permuted sequences.
      For example, number_samples = 5

  Returns:
    sampled_index_sequences: (a list of numpy arrays)
      - a list of subsampled block-preserving permuted sequences.
      For example, sampled_index_sequences =
      [[10,11,12,1,2,6],
       [6,1,2,10,11,12],
       [1,2,10,11,12,6],
       [6,1,2,10,11,12],
       [1,2,6,10,11,12]]
      The length of "sampled_index_sequences" is "number_samples".
  """
  segments = []
  if len(index_sequence) == 1:
    segments.append(index_sequence)
  else:
    prev = 0
    for i in range(len(index_sequence) - 1):
      if index_sequence[i + 1] != index_sequence[i] + 1:
        segments.append(index_sequence[prev:(i + 1)])
        prev = i + 1
      if i + 1 == len(index_sequence) - 1:
        segments.append(index_sequence[prev:])
  # sample permutations
  sampled_index_sequences = []
  for _ in range(number_samples):
    segments_array = []
    permutation = np.random.permutation(len(segments))
    for i in range(len(permutation)):
      segments_array.append(segments[permutation[i]])
    sampled_index_sequences.append(np.concatenate(segments_array))
  return sampled_index_sequences


def resize_sequence(sequence, cluster_id, num_permutations=None):
  """Resize sequences for packing and batching.

  Args:
    sequence: (real numpy matrix, size: seq_len*obs_size) - observed sequence
    cluster_id: (numpy vector, size: seq_len) - cluster indicator sequence
    num_permutations: int - Number of permutations per utterance sampled.

  Returns:
    sub_sequences: A list of numpy array, with obsevation vector from the same
      cluster in the same list.
    seq_lengths: The length of each cluster (+1).
    bias: Flipping coin head probability.
  """
  # merge sub-sequences that belong to a single cluster to a single sequence
  unique_id = np.unique(cluster_id)
  sub_sequences = []
  seq_lengths = []
  if num_permutations and num_permutations > 1:
    for i in unique_id:
      idx_set = np.where(cluster_id == i)[0]
      sampled_idx_sets = sample_permuted_segments(idx_set, num_permutations)
      for j in range(num_permutations):
        sub_sequences.append(sequence[sampled_idx_sets[j], :])
        seq_lengths.append(len(idx_set) + 1)
  else:
    for i in unique_id:
      idx_set = np.where(cluster_id == i)
      sub_sequences.append(sequence[idx_set, :][0])
      seq_lengths.append(len(idx_set[0]) + 1)

  # compute bias
  transit_num = 0
  for entry in range(len(cluster_id) - 1):
    transit_num += (cluster_id[entry] != cluster_id[entry + 1])
  bias = (transit_num + 1) / len(cluster_id)
  return sub_sequences, seq_lengths, bias


def pack_sequence(
    sub_sequences, seq_lengths, batch_size, observation_dim, device):
  """Pack sequences for training.

  Args:
    sub_sequences: A list of numpy array, with obsevation vector from the same
      cluster in the same list.
    seq_lengths: The length of each cluster (+1).
    batch_size: int or None - Run batch learning if batch_size is None. Else,
      run online learning with specified batch size.
    observation_dim: int - dimension for observation vectors
    device: str - Your device. E.g., 'cuda:0' or 'cpu'.

  Returns:
    packed_rnn_input: (PackedSequence object) packed rnn input
    rnn_truth: ground truth
  """
  num_clusters = len(seq_lengths)
  sorted_seq_lengths = np.sort(seq_lengths)[::-1]
  permute_index = np.argsort(seq_lengths)[::-1]

  if batch_size is None:
    rnn_input = np.zeros((sorted_seq_lengths[0],
                          num_clusters,
                          observation_dim))
    for i in range(num_clusters):
      rnn_input[1:sorted_seq_lengths[i], i, :] = sub_sequences[permute_index[i]]
    rnn_input = autograd.Variable(
        torch.from_numpy(rnn_input).float()).to(device)
    packed_rnn_input = torch.nn.utils.rnn.pack_padded_sequence(
        rnn_input, sorted_seq_lengths, batch_first=False)
  else:
    mini_batch = np.sort(np.random.choice(num_clusters, batch_size))
    rnn_input = np.zeros((sorted_seq_lengths[mini_batch[0]],
                          batch_size,
                          observation_dim))
    for i in range(batch_size):
      rnn_input[1:sorted_seq_lengths[mini_batch[i]],
                i, :] = sub_sequences[permute_index[mini_batch[i]]]
    rnn_input = autograd.Variable(
        torch.from_numpy(rnn_input).float()).to(device)
    packed_rnn_input = torch.nn.utils.rnn.pack_padded_sequence(
        rnn_input, sorted_seq_lengths[mini_batch], batch_first=False)
  # ground truth is the shifted input
  rnn_truth = rnn_input[1:, :, :]
  return packed_rnn_input, rnn_truth


def output_result(model_args, training_args, test_record):
  accuracy_array, _ = zip(*test_record)
  total_accuracy = np.mean(accuracy_array)
  output_string = """
Config:
  sigma_alpha: {}
  sigma_beta: {}
  crp_alpha: {}
  learning rate: {}
  learning rate half life: {}
  regularization: {}
  batch size: {}

Performance:
  averaged accuracy: {:.6f}
  accuracy numbers for all testing sequences:
  """.strip().format(
      training_args.sigma_alpha,
      training_args.sigma_beta,
      model_args.crp_alpha,
      training_args.learning_rate,
      training_args.learning_rate_half_life,
      training_args.regularization_weight,
      training_args.batch_size,
      total_accuracy)
  for accuracy in accuracy_array:
    output_string += '\n    {:.6f}'.format(accuracy)
  output_string += '\n' + '=' * 80 + '\n'
  filename = 'layer_{}_{}_{:.1f}_result.txt'.format(
      model_args.rnn_hidden_size,
      model_args.rnn_depth, model_args.rnn_dropout)
  with open(filename, 'a') as file_object:
    file_object.write(output_string)
  return output_string
