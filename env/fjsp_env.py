import sys
import gym      # type: ignore
import torch

from dataclasses import dataclass
from env.load_data import load_fjs, nums_detec
# import numpy as np                        # used in np.argwhere(); question: why return tensor when the input is tensor? why accept tensor as input?
import matplotlib.pyplot as plt             # type: ignore
import matplotlib.patches as mpatches       # type: ignore
import random
import copy
from utils.my_utils import read_json, write_json        # type: ignore
from .case_generator import CaseGenerator
from typing import Dict, Any, Union, List


@dataclass
class EnvState:
    '''
    Class for the state of the environment
    '''
    # static
    opes_appertain_batch: torch.Tensor = None           # type: ignore              # The mapping from operations to the jobs they belong to
    ope_pre_adj_batch: torch.Tensor = None              # type: ignore              # Adjacency matrix of operations and operations (precedence)
    ope_sub_adj_batch: torch.Tensor = None              # type: ignore              # Adjacency matrix of operations and operations (direct successive in the subsequence)
    end_ope_biases_batch: torch.Tensor = None           # type: ignore              # The id of the last operation of each job
    nums_opes_batch: torch.Tensor = None                # type: ignore              # The number of operations for each instance (the whole instance, not only some job)

    # dynamic
    batch_idxes: torch.Tensor = None                    # type: ignore              # Uncompleted instances
    feat_opes_batch: torch.Tensor = None                # type: ignore              # Operation features
    feat_mas_batch: torch.Tensor = None                 # type: ignore              # Machine features
    proc_times_batch: torch.Tensor = None               # type: ignore              # Processing time
    ope_ma_adj_batch: torch.Tensor = None               # type: ignore              # Adjacency matrix of operations and machines (assignment & possible assignment)

    mask_job_procing_batch: torch.Tensor = None         # type: ignore              # bool_vec for jobs in process
    mask_job_finish_batch: torch.Tensor = None          # type: ignore              # bool_vec for completed jobs, complementation of `mask_job_procing_batch`
    mask_ma_procing_batch: torch.Tensor = None          # type: ignore              # bool_vec for machines in process
    ope_step_batch: torch.Tensor = None                 # type: ignore              # The id of the current operation (be waiting to be processed) of each job
    time_batch:  torch.Tensor = None                    # type: ignore              # Current time of each instance in the environment

    def update(self,
               batch_idxes: torch.Tensor,
               feat_opes_batch: torch.Tensor,
               feat_mas_batch: torch.Tensor,
               proc_times_batch: torch.Tensor,
               ope_ma_adj_batch: torch.Tensor,
               mask_job_procing_batch: torch.Tensor,
               mask_job_finish_batch: torch.Tensor,
               mask_ma_procing_batch: torch.Tensor,
               ope_step_batch: torch.Tensor,
               time_batch: torch.Tensor):
        '''
        Only update the dynamic variables
        '''
        self.batch_idxes = batch_idxes
        self.feat_opes_batch = feat_opes_batch
        self.feat_mas_batch = feat_mas_batch
        self.proc_times_batch = proc_times_batch
        self.ope_ma_adj_batch = ope_ma_adj_batch

        self.mask_job_procing_batch = mask_job_procing_batch
        self.mask_job_finish_batch = mask_job_finish_batch
        self.mask_ma_procing_batch = mask_ma_procing_batch
        self.ope_step_batch = ope_step_batch
        self.time_batch = time_batch

def convert_feat_job_2_ope(feat_job_batch: torch.Tensor, opes_appertain_batch: torch.Tensor):
    '''
    Convert job features into operation features (such as dimension)
    '''
    # result[i,j] = feat_job_batch[i, opes_appertain_batch[i,j]]
    return feat_job_batch.gather(1, opes_appertain_batch)

class FJSPEnv(gym.Env):
    '''
    FJSP environment
    '''
    def __init__(self, case: Union[CaseGenerator, List[str]], env_paras: Dict[str, Any], data_source='case'):
        '''
        :param case: The instance generator or the addresses of the instances
        :param env_paras: A dictionary of parameters for the environment
        :param data_source: Indicates that the instances came from a generator or files

        Question: what's the difference between the `features` and `states` here?
        '''

        # load paras
        # static
        self.show_mode = env_paras["show_mode"]     # Result display mode (deprecated in the final experiment)
        self.batch_size = env_paras["batch_size"]   # Number of parallel instances during training
        self.num_jobs = env_paras["num_jobs"]       # Number of jobs
        self.num_mas = env_paras["num_mas"]         # Number of machines
        self.paras = env_paras                      # Parameters; in-place modification for parameters storage
        self.device = env_paras["device"]           # Computing device for PyTorch
        # load instance
        num_data = 8                                # The amount of data extracted from instance
        tensors: List[List[torch.Tensor]] = [[] for _ in range(num_data)]
        self.num_opes = 0
        lines: List[List[str]] = []      # actually a list of list (i.e list of lines)
        if data_source=='case':  # Generate instances through generators
            for i in range(self.batch_size):
                lines.append(case[i][0])                                      # type: ignore
                # ↑ Generate an instance and save it, using case.get_case()
                num_jobs, num_mas, num_opes = nums_detec(lines[i])
                # Records the maximum number of operations in the parallel instances
                self.num_opes = max(self.num_opes, num_opes)
        else:  # Load instances from files
            for i in range(self.batch_size):
                with open(case[i]) as file_object:
                    line = file_object.readlines()
                    lines.append(line)
                num_jobs, num_mas, num_opes = nums_detec(lines[i])
                self.num_opes = max(self.num_opes, num_opes)
        # load feats
        for i in range(self.batch_size):
            load_data = load_fjs(lines[i], num_mas, self.num_opes)
            for j in range(num_data):
                tensors[j].append(load_data[j])

        # dynamic feats
        # ↓ shape: (batch_size, num_opes, num_mas)
        self.proc_times_batch = torch.stack(tensors[0], dim=0)  # also .float()?    # processing time of each operation on each machine
        # ↑ it's some dynamic feature, as when some operations are scheduled, only the selected machine's processing time is retained
        # ↓ shape: (batch_size, num_opes, num_mas)
        self.ope_ma_adj_batch = torch.stack(tensors[1], dim=0).long()               # adjacency matrix of operations and machines
        # shape: (batch_size, num_opes, num_opes), for calculating the cumulative amount along the path of each job
        self.cal_cumul_adj_batch = torch.stack(tensors[7], dim=0).float()
        # ↑ adjacency matrix of operations and operations (for calculating the cumulative amount along the path of each job)

        # static feats
        # ↓ shape: (batch_size, num_opes, num_opes)
        self.ope_pre_adj_batch = torch.stack(tensors[2], dim=0)             # adjacency matrix of operations and operations (precedence)    
        # ↓ shape: (batch_size, num_opes, num_opes)
        self.ope_sub_adj_batch = torch.stack(tensors[3], dim=0)             # adjacency matrix of operations and operations (direct successive in the subsequence)
        # ↓ shape: (batch_size, num_opes), represents the mapping between operations and jobs
        self.opes_appertain_batch = torch.stack(tensors[4], dim=0).long()   # "appertain" means "belong to"; the mapping between operations and jobs
        # ↓ shape: (batch_size, num_jobs), the id of the first operation of each job
        self.num_ope_biases_batch = torch.stack(tensors[5], dim=0).long()   # the id of the first operation of each job
        # ↓ shape: (batch_size, num_jobs), the number of operations for each job
        self.nums_ope_batch = torch.stack(tensors[6], dim=0).long()                         # the number of operations for each job
        # ↓ shape: (batch_size, num_jobs), the id of the last operation of each job
        self.end_ope_biases_batch = self.num_ope_biases_batch + self.nums_ope_batch - 1     # the id of the last operation of each job
        # ↓ shape: (batch_size), the number of operations for each instance
        self.nums_opes = torch.sum(self.nums_ope_batch, dim=1)

        # dynamic variables
        self.batch_idxes = torch.arange(self.batch_size)                # Uncompleted instances
        self.time = torch.zeros(self.batch_size)                        # Current time of the instance in the environment
        self.N = torch.zeros(self.batch_size).int()                     # Count scheduled operations
        # ↓ shape: (batch_size, num_jobs)
        self.ope_step_batch = copy.deepcopy(self.num_ope_biases_batch)  # the id of the current operation (be waiting to be processed) of each job
        '''
        features, dynamic
            ope:
                Status (1: scheduled, 0: not scheduled, until step t)                               feat_opes_batch[:, 0, :]
                Number of neighboring machines                                                      feat_opes_batch[:, 1, :]
                Processing time                                                                     feat_opes_batch[:, 2, :]
                Number of unscheduled operations in the job that the operation belongs to           feat_opes_batch[:, 3, :]
                Job completion time (the job that the operation belongs to)                         feat_opes_batch[:, 4, :]
                Start time                                                                          feat_opes_batch[:, 5, :]
            ma:
                Number of neighboring operations                                                    feat_mas_batch[:, 0, :]
                Available time                                                                      feat_mas_batch[:, 1, :]
                Utilization                                                                         feat_mas_batch[:, 2, :]
        '''
        # Generate raw feature vectors
        feat_opes_batch = torch.zeros(size=(self.batch_size, self.paras["ope_feat_dim"], self.num_opes))                # vertical vector feature
        feat_mas_batch = torch.zeros(size=(self.batch_size, self.paras["ma_feat_dim"], num_mas))                        # vertical vector feature

        # ↓ Features of operations
        # feat_opes_batch[:, 0, :] already initialized to 0                                                             # Status
        feat_opes_batch[:, 1, :] = torch.count_nonzero(self.ope_ma_adj_batch, dim=2)                                    # Number of neighboring machines
        feat_opes_batch[:, 2, :] = torch.sum(self.proc_times_batch, dim=2).div(feat_opes_batch[:, 1, :] + 1e-9)         # Operation Processing time (average version, for each operation until scheduled)
        feat_opes_batch[:, 3, :] = convert_feat_job_2_ope(self.nums_ope_batch, self.opes_appertain_batch)               # Number of unscheduled operations in the job
                                                                                                                        # that the operation belongs to (repeated usage)
        feat_opes_batch[:, 5, :] = torch.bmm(feat_opes_batch[:, 2, :].unsqueeze(1),                                     # ↑ (originally all unscheduled)
                                             self.cal_cumul_adj_batch).squeeze()                                        # Operation Start time                  
        end_time_batch = (feat_opes_batch[:, 5, :] +
                          feat_opes_batch[:, 2, :]).gather(1, self.end_ope_biases_batch)                                # Job completion time (prepare to use)
        # ↑ https://pytorch.org/docs/stable/generated/torch.gather.html
        # i.e. end_time_batch[i,j] = sum_result[i, end_ope_biases_batch[i,j]]
        feat_opes_batch[:, 4, :] = convert_feat_job_2_ope(end_time_batch, self.opes_appertain_batch)                    # Job completion time of operations (repeated usage)

        # ↓ Features of machines
        feat_mas_batch[:, 0, :] = torch.count_nonzero(self.ope_ma_adj_batch, dim=1)                                     # Number of neighboring operations (of machines)
        # feat_mas_batch[:, 1, :] already initialized to 0                                                              # Available time (of machines)
        # feat_mas_batch[:, 2, :] already initialized to 0                                                              # Utilization (of machines)

        # ↓ Two features write back
        self.feat_opes_batch = feat_opes_batch                                                                          # Operation features
        self.feat_mas_batch = feat_mas_batch                                                                            # Machine features

        # Masks of current status, dynamic
        # shape: (batch_size, num_jobs), True for jobs in process
        self.mask_job_procing_batch = torch.full(size=(self.batch_size, num_jobs), dtype=torch.bool, fill_value=False)  # bool_vec for jobs in process
        # shape: (batch_size, num_jobs), True for completed jobs
        self.mask_job_finish_batch = torch.full(size=(self.batch_size, num_jobs), dtype=torch.bool, fill_value=False)   # bool_vec for completed jobs, complementation of `mask_job_procing_batch`
        # shape: (batch_size, num_mas), True for machines in process
        self.mask_ma_procing_batch = torch.full(size=(self.batch_size, num_mas), dtype=torch.bool, fill_value=False)    # bool_vec for machines in process
        '''
        Partial Schedule (state) of jobs/operations, dynamic                                                            would be used to calculate the reward
            Status
            Allocated machines
            Start time
            End time
        '''
        self.schedules_batch = torch.zeros(size=(self.batch_size, self.num_opes, 4))                                    # Status, Allocated machines, Start time, End time
        # self.schedules_batch[:, :, 0] already initialized to 0                                                        # Status
        # self.schedules_batch[:, :, 1] already initialized to 0                                                        # Allocated machines (question: why 0 is okay for initialization? Since 0 is for the first one)
        self.schedules_batch[:, :, 2] = feat_opes_batch[:, 5, :]                                                        # Start time
        self.schedules_batch[:, :, 3] = feat_opes_batch[:, 5, :] + feat_opes_batch[:, 2, :]                             # End time
        '''
        Partial Schedule (state) of machines, dynamic
            idle (0: not idle, 1: idle)
            available_time
            utilization_time
            id_ope
        '''
        self.machines_batch = torch.zeros(size=(self.batch_size, self.num_mas, 4))                                      # idle, available_time, utilization_time, id_ope
        self.machines_batch[:, :, 0] = torch.ones(size=(self.batch_size, self.num_mas))                                 # idle
        # self.machines_batch[:, :, 1] already initialized to 0                                                         # available_time
        # self.machines_batch[:, :, 2] already initialized to 0                                                         # utilization_time
        # self.machines_batch[:, :, 3] already initialized to 0                                                         # id_ope (question again: why 0 is okay for initialization? Since 0 is for the first one)

        # ↓ Makespan and completion signal of each instance, dynamic
        self.makespan_batch: torch.Tensor = torch.max(self.feat_opes_batch[:, 4, :], dim=1)[0]                          # shape: (batch_size,), feat_opes_batch[:, 4, :] is for job completion time of operations
        self.done_batch: torch.Tensor = self.mask_job_finish_batch.all(dim=1)                                           # shape: (batch_size,), True for all job completed of some instance

        self.state = EnvState(batch_idxes=self.batch_idxes,
                              feat_opes_batch=self.feat_opes_batch,
                              feat_mas_batch=self.feat_mas_batch,
                              proc_times_batch=self.proc_times_batch,
                              ope_ma_adj_batch=self.ope_ma_adj_batch,
                              ope_pre_adj_batch=self.ope_pre_adj_batch,
                              ope_sub_adj_batch=self.ope_sub_adj_batch,
                              mask_job_procing_batch=self.mask_job_procing_batch,
                              mask_job_finish_batch=self.mask_job_finish_batch,
                              mask_ma_procing_batch=self.mask_ma_procing_batch,
                              opes_appertain_batch=self.opes_appertain_batch,
                              ope_step_batch=self.ope_step_batch,
                              end_ope_biases_batch=self.end_ope_biases_batch,
                              time_batch=self.time, nums_opes_batch=self.nums_opes)

        # Save initial data for reset
        self.old_proc_times_batch = copy.deepcopy(self.proc_times_batch)            # why? it's already in the state
        self.old_ope_ma_adj_batch = copy.deepcopy(self.ope_ma_adj_batch)            # why? it's already in the state
        self.old_cal_cumul_adj_batch = copy.deepcopy(self.cal_cumul_adj_batch)
        self.old_feat_opes_batch = copy.deepcopy(self.feat_opes_batch)              # why? it's already in the state
        self.old_feat_mas_batch = copy.deepcopy(self.feat_mas_batch)                # why? it's already in the state
        self.old_state = copy.deepcopy(self.state)

    def step(self, actions: torch.Tensor):
        '''
        Environment transition function
        
        Shape of `actions`: (3, batch_size)
        Please check PPO_model.HGNNScheduler.act() for more details

        The `opes` can already determine the `jobs`, but we keep the `jobs` for convenience

        For the "Removed unselected O-M arcs of the scheduled operations":
        below is the one line version of implementation in replacing those unselected O-M arcs of the scheduled operations to be 0 given the selected operations

        self.ope_ma_adj_batch[self.batch_idxes, opes] = torch.zeros(size=(self.batch_size, self.num_mas), dtype=torch.int64).index_put_((self.batch_idxes, mas), torch.tensor(1, dtype=torch.int64)) # using broadcasting, but could not use 1 for torch.tensor(1, dtype=torch.int64) directly

        self.ope_ma_adj_batch[self.batch_idxes, opes] = torch.zeros(size=(self.batch_size, self.num_mas), dtype=torch.int64).index_put_((self.batch_idxes, mas), torch.ones(len(self.batch_idxes), dtype=torch.int64))  # no broadcasting version
        # torch.Tensor.index_put(): out-place version; torch.Tensor.index_put_(): in-place version

        all the updates are carried out over those active instances in the batch, i.e. unfinished ones, marked by `self.batch_idxes`
        '''
        opes = actions[0, :]            # the outer, or say, the flattened operation idx, not the intra-index of each job, shape: (batch_size,), a row vector
        mas = actions[1, :]             # the machine idx, shape: (batch_size,), a row vector
        jobs = actions[2, :]            # the job idx, shape: (batch_size,), a row vector
        self.N += 1                     # Count scheduled operations

        # Removed unselected O-M arcs of the scheduled operations
        remain_ope_ma_adj = torch.zeros(size=(self.batch_size, self.num_mas), dtype=torch.int64)
        remain_ope_ma_adj[self.batch_idxes, mas] = 1        # mark those machine-used's link as 1; those not-used's link as 0 (default value)
        # ↑ It's a bit weird to use this intermediate variable, but it's convenient to reuse such a variable
        # - It actually provide a slice, no matter what the operation is (to be determined on the left hand side), it ensure that
        #   those machine to be used are marked as 1, and those not to be used are marked as 0
        # - Also note that each slice will not be reused, each can be referred to by the (operation, machine) pair in the input
        # ↓ shape of `ope_ma_adj_batch`: (batch_size, num_opes, num_mas)
        self.ope_ma_adj_batch[self.batch_idxes, opes] = remain_ope_ma_adj[self.batch_idxes, :]
        # self.ope_ma_adj_batch[self.batch_idxes, opes] = torch.zeros(size=(self.batch_size, self.num_mas), dtype=torch.int64).index_put_((self.batch_idxes, mas), torch.tensor(1, dtype=torch.int64))
        self.proc_times_batch *= self.ope_ma_adj_batch      # use the adjacency matrix to filter out the processing time of the unselected O-M arcs
        # ↑ Those without assignment link will be masked to be 0

        # Update for some O-M arcs are removed, such as 'Status', 'Number of neighboring machines' and 'Processing time'
        proc_times = self.proc_times_batch[self.batch_idxes, opes, mas]
        self.feat_opes_batch[self.batch_idxes, :3, opes] = torch.stack((torch.ones(self.batch_idxes.size(0), dtype=torch.float),
                                                                        torch.ones(self.batch_idxes.size(0), dtype=torch.float),
                                                                        proc_times), dim=1)
        last_opes = torch.where(opes - 1 < self.num_ope_biases_batch[self.batch_idxes, jobs], self.num_opes - 1, opes - 1)
        # Determine the shape of the slice that's indexing
        slice_shape = self.cal_cumul_adj_batch[self.batch_idxes, last_opes, :].shape
        # Create a tensor of zeros with the same shape
        zeros = torch.zeros(slice_shape, device=self.cal_cumul_adj_batch.device)
        self.cal_cumul_adj_batch[self.batch_idxes, last_opes, :] = zeros    # may directly set to be 0, but could cause error in deterministic mode
        # ↑ broadcasting doesn't work in deterministic mode, or we can just set self.cal_cumul_adj_batch[self.batch_idxes, last_opes, :] = 0

        # Update 'Number of unscheduled operations in the job'
        start_ope = self.num_ope_biases_batch[self.batch_idxes, jobs]       # those starting operation idx of the jobs in those unfinished instances in the batch
        end_ope = self.end_ope_biases_batch[self.batch_idxes, jobs]         # those ending operation idx of the jobs in those unfinished instances in the batch
        for i in range(self.batch_idxes.size(0)):
            self.feat_opes_batch[self.batch_idxes[i], 3, start_ope[i]:end_ope[i]+1] -= 1    # type: ignore  # `3` for 'Number of unscheduled operations in the job'

        # Update 'Start time' and 'Job completion time'
        self.feat_opes_batch[self.batch_idxes, 5, opes] = self.time[self.batch_idxes]       # '5' for 'Start time', using the current time of the instance in the environment
        is_scheduled = self.feat_opes_batch[self.batch_idxes, 0, :]                         # '0' for 'Status', the `is_scheduled` is for the operation
        mean_proc_time = self.feat_opes_batch[self.batch_idxes, 2, :]                       # '2' for 'Processing time', the `mean_proc_time` is for the operation (average over feasible machines)
        start_times = self.feat_opes_batch[self.batch_idxes, 5, :] * is_scheduled           # real start time of scheduled opes, shape: (len(self.batch_idxes), 1, self.num_opes)
        un_scheduled = 1 - is_scheduled                                                     # unscheduled opes, also okay to use the ~is_scheduled.bool()
        estimate_times = torch.bmm((start_times + mean_proc_time).unsqueeze(1),                         # need to check the `load_fjs()` for details
                                    self.cal_cumul_adj_batch[self.batch_idxes, :, :]).squeeze()\
                                    * un_scheduled                                                      # estimate start time of unscheduled opes
        # ↑ https://pytorch.org/docs/stable/generated/torch.bmm.html, bmm for "batch matrix multiplication", out_i = mat1_i @ mat2_i
        self.feat_opes_batch[self.batch_idxes, 5, :] = start_times + estimate_times
        end_time_batch = (self.feat_opes_batch[self.batch_idxes, 5, :] +
                          self.feat_opes_batch[self.batch_idxes, 2, :]).gather(1, self.end_ope_biases_batch[self.batch_idxes, :])
        self.feat_opes_batch[self.batch_idxes, 4, :] = convert_feat_job_2_ope(end_time_batch, self.opes_appertain_batch[self.batch_idxes,:])

        # Update partial schedule (state)
        self.schedules_batch[self.batch_idxes, opes, :2] = torch.stack((torch.ones(self.batch_idxes.size(0)), mas), dim=1)
        self.schedules_batch[self.batch_idxes, :, 2] = self.feat_opes_batch[self.batch_idxes, 5, :]
        self.schedules_batch[self.batch_idxes, :, 3] = self.feat_opes_batch[self.batch_idxes, 5, :] + \
                                                       self.feat_opes_batch[self.batch_idxes, 2, :]
        self.machines_batch[self.batch_idxes, mas, 0] = torch.zeros(self.batch_idxes.size(0))
        self.machines_batch[self.batch_idxes, mas, 1] = self.time[self.batch_idxes] + proc_times
        self.machines_batch[self.batch_idxes, mas, 2] += proc_times
        self.machines_batch[self.batch_idxes, mas, 3] = jobs.float()

        # Update feature vectors of machines
        self.feat_mas_batch[self.batch_idxes, 0, :] = torch.count_nonzero(self.ope_ma_adj_batch[self.batch_idxes, :, :], dim=1).float()
        self.feat_mas_batch[self.batch_idxes, 1, mas] = self.time[self.batch_idxes] + proc_times
        utiliz = self.machines_batch[self.batch_idxes, :, 2]
        cur_time = self.time[self.batch_idxes, None].expand_as(utiliz)
        utiliz = torch.minimum(utiliz, cur_time)
        utiliz = utiliz.div(self.time[self.batch_idxes, None] + 1e-9)
        self.feat_mas_batch[self.batch_idxes, 2, :] = utiliz

        # Update other variable according to actions
        self.ope_step_batch[self.batch_idxes, jobs] += 1
        self.mask_job_procing_batch[self.batch_idxes, jobs] = True
        self.mask_ma_procing_batch[self.batch_idxes, mas] = True
        self.mask_job_finish_batch = torch.where(self.ope_step_batch==self.end_ope_biases_batch+1,
                                                 True, self.mask_job_finish_batch)
        self.done_batch = self.mask_job_finish_batch.all(dim=1)
        self.done = self.done_batch.all()

        max_val: torch.Tensor = torch.max(self.feat_opes_batch[:, 4, :], dim=1)[0]
        self.reward_batch: torch.Tensor = self.makespan_batch - max_val
        self.makespan_batch = max_val

        # Check if there are still O-M pairs to be processed, otherwise the environment transits to the next time
        flag_trans_2_next_time = self.if_no_eligible()
        while ~((~((flag_trans_2_next_time==0) & (~self.done_batch))).all()):
            self.next_time(flag_trans_2_next_time)
            flag_trans_2_next_time = self.if_no_eligible()

        # Update the vector for uncompleted instances
        mask_finish = (self.N+1) <= self.nums_opes      # for uncompleted, not for completed
        if ~(mask_finish.all()):                        # not all completed
            self.batch_idxes = torch.arange(self.batch_size)[mask_finish]

        # Update state of the environment
        self.state.update(self.batch_idxes, self.feat_opes_batch, self.feat_mas_batch, self.proc_times_batch,
            self.ope_ma_adj_batch, self.mask_job_procing_batch, self.mask_job_finish_batch, self.mask_ma_procing_batch,
                          self.ope_step_batch, self.time)
        return self.state, self.reward_batch, self.done_batch

    def if_no_eligible(self):
        '''
        Check if there are still O-M pairs to be processed
        '''
        ope_step_batch = torch.where(self.ope_step_batch > self.end_ope_biases_batch,
                                     self.end_ope_biases_batch, self.ope_step_batch)
        op_proc_time = self.proc_times_batch.gather(1, ope_step_batch.unsqueeze(-1).expand(-1, -1,
                                                                                        self.proc_times_batch.size(2)))
        ma_eligible = ~self.mask_ma_procing_batch.unsqueeze(1).expand_as(op_proc_time)
        job_eligible = ~(self.mask_job_procing_batch + self.mask_job_finish_batch)[:, :, None].expand_as(
            op_proc_time)
        flag_trans_2_next_time = torch.sum(torch.where(ma_eligible & job_eligible, op_proc_time.double(), 0.0).transpose(1, 2),
                                           dim=[1, 2])
        # shape: (batch_size,), a boolean Tensor
        # An element value of 0 means that the corresponding instance has no eligible O-M pairs
        # in other words, the environment need to transit to the next time
        return flag_trans_2_next_time

    def next_time(self, flag_trans_2_next_time: torch.Tensor):
        '''
        Transit to the next time
        '''
        # need to transit
        flag_need_trans = (flag_trans_2_next_time==0) & (~self.done_batch)
        # available_time of machines
        a = self.machines_batch[:, :, 1]
        # remain available_time greater than current time
        b = torch.where(a > self.time[:, None], a, torch.max(self.feat_opes_batch[:, 4, :]) + 1.0)
        # Return the minimum value of available_time (the time to transit to)
        c = torch.min(b, dim=1)[0]
        # Detect the machines that completed (at above time)
        d = torch.where((a == c[:, None]) & (self.machines_batch[:, :, 0] == 0) & flag_need_trans[:, None], True, False)
        # The time for each batch to transit to or stay in
        e = torch.where(flag_need_trans, c, self.time)
        self.time = e

        # Update partial schedule (state), variables and feature vectors
        aa = self.machines_batch.transpose(1, 2)
        aa[d, 0] = 1
        self.machines_batch = aa.transpose(1, 2)

        utiliz = self.machines_batch[:, :, 2]
        cur_time = self.time[:, None].expand_as(utiliz)
        utiliz = torch.minimum(utiliz, cur_time)
        utiliz = utiliz.div(self.time[:, None] + 1e-5)
        self.feat_mas_batch[:, 2, :] = utiliz

        jobs = torch.where(d, self.machines_batch[:, :, 3].double(), -1.0).float()
        # jobs_index = np.argwhere(jobs.cpu() >= 0).to(self.device)       # type: ignore # question: why np.argwhere also work? and torch.argwhere doesn't work?
        jobs_index = torch.nonzero(jobs >= 0, as_tuple=True)              # https://github.com/pytorch/pytorch/issues/64502
        # ↑ torch.nonzero is similar to np.argwhere not np.nonzero

        job_idxes = jobs[jobs_index[0], jobs_index[1]].long()
        batch_idxes = jobs_index[0]

        self.mask_job_procing_batch[batch_idxes, job_idxes] = False
        self.mask_ma_procing_batch[d] = False
        self.mask_job_finish_batch = torch.where(self.ope_step_batch == self.end_ope_biases_batch + 1,
                                                 True, self.mask_job_finish_batch)

    def reset(self):
        '''
        Reset the environment to its initial state
        '''
        self.proc_times_batch = copy.deepcopy(self.old_proc_times_batch)
        self.ope_ma_adj_batch = copy.deepcopy(self.old_ope_ma_adj_batch)
        self.cal_cumul_adj_batch = copy.deepcopy(self.old_cal_cumul_adj_batch)
        self.feat_opes_batch = copy.deepcopy(self.old_feat_opes_batch)
        self.feat_mas_batch = copy.deepcopy(self.old_feat_mas_batch)
        self.state = copy.deepcopy(self.old_state)

        self.batch_idxes = torch.arange(self.batch_size)
        self.time = torch.zeros(self.batch_size)
        self.N = torch.zeros(self.batch_size)
        self.ope_step_batch = copy.deepcopy(self.num_ope_biases_batch)
        self.mask_job_procing_batch = torch.full(size=(self.batch_size, self.num_jobs), dtype=torch.bool, fill_value=False)
        self.mask_job_finish_batch = torch.full(size=(self.batch_size, self.num_jobs), dtype=torch.bool, fill_value=False)
        self.mask_ma_procing_batch = torch.full(size=(self.batch_size, self.num_mas), dtype=torch.bool, fill_value=False)
        self.schedules_batch = torch.zeros(size=(self.batch_size, self.num_opes, 4))
        self.schedules_batch[:, :, 2] = self.feat_opes_batch[:, 5, :]
        self.schedules_batch[:, :, 3] = self.feat_opes_batch[:, 5, :] + self.feat_opes_batch[:, 2, :]
        self.machines_batch = torch.zeros(size=(self.batch_size, self.num_mas, 4))
        self.machines_batch[:, :, 0] = torch.ones(size=(self.batch_size, self.num_mas))

        self.makespan_batch = torch.max(self.feat_opes_batch[:, 4, :], dim=1)[0]
        self.done_batch = self.mask_job_finish_batch.all(dim=1)
        return self.state

    def render(self, mode='draw'):
        '''
        Deprecated in the final experiment
        '''
        if self.show_mode is None and mode is None:
            mode = 'draw'
        elif self.show_mode is not None and mode is None:
            mode = self.show_mode
        if mode == 'draw':
            num_jobs = self.num_jobs
            num_mas = self.num_mas
            print(sys.argv[0])
            color = read_json("./utils/color_config")["gantt_color"]
            if len(color) < num_jobs:
                num_append_color = num_jobs - len(color)
                color += ['#' + ''.join([random.choice("0123456789ABCDEF") for _ in range(6)]) for c in
                          range(num_append_color)]
            write_json({"gantt_color": color}, "./utils/color_config")
            for batch_id in range(self.batch_size):
                schedules = self.schedules_batch[batch_id].to('cpu')
                fig = plt.figure(figsize=(10, 6))
                fig.canvas.set_window_title('Visual_gantt')
                axes = fig.add_axes([0.1, 0.1, 0.72, 0.8])
                y_ticks = []
                y_ticks_loc = []
                for i in range(num_mas):
                    y_ticks.append('Machine {0}'.format(i))
                    y_ticks_loc.insert(0, i + 1)
                labels = [''] * num_jobs
                for j in range(num_jobs):
                    labels[j] = "job {0}".format(j + 1)
                patches = [mpatches.Patch(color=color[k], label="{:s}".format(labels[k])) for k in range(self.num_jobs)]
                axes.cla()
                axes.set_title(u'FJSP Schedule')
                axes.grid(linestyle='-.', color='gray', alpha=0.2)
                axes.set_xlabel('Time')
                axes.set_ylabel('Machine')
                axes.set_yticks(y_ticks_loc, y_ticks)
                axes.legend(handles=patches, loc=2, bbox_to_anchor=(1.01, 1.0), fontsize=int(14 / pow(1, 0.3)))
                axes.set_ybound(1 - 1 / num_mas, num_mas + 1 / num_mas)
                for i in range(int(self.nums_opes[batch_id])):
                    id_ope = i
                    idx_job, idx_ope = self.get_idx(id_ope, batch_id)
                    id_machine = schedules[id_ope][1]
                    axes.barh(id_machine,
                             0.2,
                             left=schedules[id_ope][2],
                             color='#b2b2b2',
                             height=0.5)
                    axes.barh(id_machine,
                             schedules[id_ope][3] - schedules[id_ope][2] - 0.2,
                             left=schedules[id_ope][2]+0.2,
                             color=color[idx_job],
                             height=0.5)
                plt.show()
        return

    def get_idx(self, id_ope, batch_id):
        '''
        Get job and operation (relative) index based on instance index and operation (absolute) index
        '''
        idx_job = max([idx for (idx, val) in enumerate(self.num_ope_biases_batch[batch_id]) if id_ope >= val])
        idx_ope = id_ope - self.num_ope_biases_batch[batch_id][idx_job]
        return idx_job, idx_ope

    def validate_gantt(self):
        '''
        Verify whether the schedule is feasible
        '''
        ma_gantt_batch = [[[] for _ in range(self.num_mas)] for __ in range(self.batch_size)]
        for batch_id, schedules in enumerate(self.schedules_batch):
            for i in range(int(self.nums_opes[batch_id])):
                step = schedules[i]
                ma_gantt_batch[batch_id][int(step[1])].append([i, step[2].item(), step[3].item()])
        proc_time_batch = self.proc_times_batch

        # Check whether there are overlaps and correct processing times on the machine
        flag_proc_time = 0
        flag_ma_overlap = 0
        flag = 0
        for k in range(self.batch_size):
            ma_gantt = ma_gantt_batch[k]
            proc_time = proc_time_batch[k]
            for i in range(self.num_mas):
                ma_gantt[i].sort(key=lambda s: s[1])
                for j in range(len(ma_gantt[i])):
                    if (len(ma_gantt[i]) <= 1) or (j == len(ma_gantt[i])-1):
                        break
                    if ma_gantt[i][j][2]>ma_gantt[i][j+1][1]:
                        flag_ma_overlap += 1
                    if ma_gantt[i][j][2]-ma_gantt[i][j][1] != proc_time[ma_gantt[i][j][0]][i]:
                        flag_proc_time += 1
                    flag += 1

        # Check job order and overlap
        flag_ope_overlap = 0
        for k in range(self.batch_size):
            schedule = self.schedules_batch[k]
            nums_ope = self.nums_ope_batch[k]
            num_ope_biases = self.num_ope_biases_batch[k]
            for i in range(self.num_jobs):
                if int(nums_ope[i]) <= 1:
                    continue
                for j in range(int(nums_ope[i]) - 1):
                    step = schedule[num_ope_biases[i]+j]
                    step_next = schedule[num_ope_biases[i]+j+1]
                    if step[3] > step_next[2]:
                        flag_ope_overlap += 1

        # Check whether there are unscheduled operations
        flag_unscheduled = 0
        for batch_id, schedules in enumerate(self.schedules_batch):
            count = 0
            for i in range(schedules.size(0)):
                if schedules[i][0]==1:
                    count += 1
            add = 0 if (count == self.nums_opes[batch_id]) else 1
            flag_unscheduled += add

        if flag_ma_overlap + flag_ope_overlap + flag_proc_time + flag_unscheduled != 0:
            return False, self.schedules_batch
        else:
            return True, self.schedules_batch

    def close(self):
        pass
