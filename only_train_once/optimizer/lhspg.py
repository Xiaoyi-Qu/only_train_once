import torch
import numpy as np
from torch.optim.optimizer import Optimizer, required
import torch.nn.functional as F

from .hyperparameter import DEFAULT_OPT_PARAMS
from .importance_score import calculate_importance_score_lora
from only_train_once.transform import tensor_transformation, TensorTransform

LORA_NAMES = [('lora_B', 'lora_A'), ('lora_embedding_B', 'lora_embedding_A')]

class LHSPG(Optimizer):
    def __init__(self, params, variant='sgd', lr=required, epsilon=0.0, save_memory=True, device=None, \
                 first_momentum=None, second_momentum=None, dampening=None, weight_decay=None, target_group_sparsity=0.5, \
                 tolerance_group_sparsity=0.05, start_pruning_step=0, pruning_steps=None, pruning_periods=None, \
                 group_divisible=1, fixed_zero_groups=True, lora_update_freq=4, importance_score_criteria=None):

        print("Setup LHSPG")
        if lr is not required and lr < 0.0:
            raise ValueError("Invalid learning rate: {}".format(lr))
        
        self.save_memory = save_memory
        self.num_steps = 0
        self.start_pruning_step = start_pruning_step
        self.pruning_steps = pruning_steps
        self.pruning_periods = int(max(1, pruning_periods)) # How many periods that the pruning last for.
        self.pruning_period_duration = self.pruning_steps // self.pruning_periods # How many pruning steps for each period
        self.curr_pruning_period = 0 # Track pruning periodp
        self.device = device

        # Set up hyper-parameters related to baseline optimizer
        first_momentum = first_momentum if first_momentum is not None else DEFAULT_OPT_PARAMS[variant]['first_momentum']
        second_momentum = second_momentum if second_momentum is not None else DEFAULT_OPT_PARAMS[variant]['second_momentum']
        dampening = dampening if dampening is not None else DEFAULT_OPT_PARAMS[variant]['dampening']
        weight_decay = weight_decay if weight_decay is not None else DEFAULT_OPT_PARAMS[variant]['weight_decay']
        
        self.redundant_groups_identified = False
        
        self.fixed_zero_groups = fixed_zero_groups
        self.lora_update_freq = lora_update_freq
        
        self.safe_guard = 1e-8
        self.target_group_sparsity = target_group_sparsity
        self.tolerance_group_sparsity = tolerance_group_sparsity
        
        self.norm_important_groups = 0.0 # norm for important groups
        self.norm_redundant_groups = 0.0 # norm for redundant groups
        self.num_important_groups = 0 # number of important groups
        self.num_redundant_groups = 0 # number of redundant groups

        self.pruned_group_idx = list()
        self.pruned_group_idx_by_cluster = dict()

        self.importance_score_criteria = importance_score_criteria

        defaults = dict(lr=lr, weight_decay=weight_decay, first_momentum=first_momentum, second_momentum=second_momentum, \
                        dampening=dampening, variant=variant, grad_variant=dict(),
                        global_start_idx=0, global_idx=0, group_divisible=group_divisible)
        
        super(LHSPG, self).__init__(params, defaults)

        self.group_divisible = group_divisible
        self.first_moment_grads = dict()
        self.second_moment_grads = dict()

        # Set up total number of prunable groups
        self.total_num_groups = 0
        self.total_num_groups_by_clusters = dict()
        self.prunable_param_group_clusters = dict()

        self.target_group_sparsity = self.target_group_sparsity if isinstance(self.target_group_sparsity, dict) else {'overall': self.target_group_sparsity}

        if isinstance(self.target_group_sparsity, dict):
            for cluster_name in self.target_group_sparsity:
                self.prunable_param_group_clusters[cluster_name] = list()
                cluster_gs = self.target_group_sparsity[cluster_name]
                for param_group in self.param_groups:
                    if not param_group['is_prunable']:
                        continue
                    in_cluster = False
                    for p_name in param_group['p_names']:
                        if cluster_name in p_name:
                            in_cluster = True
                            break
                    if in_cluster:
                        self.prunable_param_group_clusters[cluster_name].append(param_group)

        for cluster_name in self.prunable_param_group_clusters:
            param_group_cluster = self.prunable_param_group_clusters[cluster_name]
            self.total_num_groups_by_clusters[cluster_name] = 0
            for param_group in param_group_cluster:
                self.total_num_groups += param_group['num_groups']
                self.total_num_groups_by_clusters[cluster_name] += param_group['num_groups']

        print(self.total_num_groups, self.total_num_groups_by_clusters)

        # Set up target number of redundant groups
        self.target_num_redundant_groups = 0
        self.target_num_redundant_groups_by_clusters = dict()

        for cluster_name in self.prunable_param_group_clusters:
            param_group_cluster = self.prunable_param_group_clusters[cluster_name]
            self.target_num_redundant_groups_by_clusters[cluster_name] = 0
            for param_group in param_group_cluster:
                self.target_num_redundant_groups_by_clusters[cluster_name] = int(self.total_num_groups_by_clusters[cluster_name] \
                                                                              * min(self.target_group_sparsity[cluster_name], 0.999))
            self.target_num_redundant_groups += self.target_num_redundant_groups_by_clusters[cluster_name]
        print(self.target_num_redundant_groups, self.target_num_redundant_groups_by_clusters)

        # Set up active number redundant groups for each pruning period
        self.active_num_redundant_groups_by_clusters = dict()
        for cluster_name in self.prunable_param_group_clusters:
            self.active_num_redundant_groups_by_clusters[cluster_name] = dict()
            self.pruned_group_idx_by_cluster[cluster_name] = list()
            
        for cluster_name in self.prunable_param_group_clusters:
            param_group_cluster = self.prunable_param_group_clusters[cluster_name]
            groups_sum = 0
            for p in range(self.pruning_periods):
                if p == self.pruning_periods - 1:
                    self.active_num_redundant_groups_by_clusters[cluster_name][p] = self.target_num_redundant_groups_by_clusters[cluster_name] - groups_sum
                else:
                    self.active_num_redundant_groups_by_clusters[cluster_name][p] = self.target_num_redundant_groups_by_clusters[cluster_name] // self.pruning_periods
                    groups_sum += self.active_num_redundant_groups_by_clusters[cluster_name][p]

        self.important_idxes = dict()
        self.pruned_idxes = dict()
        self.active_redundant_idxes = dict()
        
        for param_group in params:
            self.important_idxes[param_group['id']] = [i for i in range(param_group['num_groups'])]
            self.pruned_idxes[param_group['id']] = list()
            self.active_redundant_idxes[param_group['id']] = list()

        self.curr_group_sparsity, _, self.curr_num_zero_groups = self.compute_group_sparsity_param_norm()

        print(self.active_num_redundant_groups_by_clusters)
        print(self.curr_group_sparsity, _, self.curr_num_zero_groups)

        # Create param dictionary for facilitating accessing lora_A modules
        self.named_parameters = dict()
        for param_group in self.param_groups:
            for (p_name, param) in zip(param_group['p_names'], param_group['params']):
                self.named_parameters[p_name] = param
        
    def __setstate__(self, state):
        super(LHSPG, self).__setstate__(state)

    def get_first_momentum_grad(self, name, first_moment, dampening, grad):
        if first_moment > 0:
            if name not in self.first_moment_grads:
                buf = self.first_moment_grads[name] = grad
            else:
                buf = self.first_moment_grads[name]
                buf.mul_(first_moment).add_(grad, alpha=(1.0-dampening))
            return buf
        else:
            return grad

    def get_second_momentum_grad_square(self, name, second_moment, dampening, grad):
        if second_moment > 0:
            if name not in self.second_moment_grads:
                buf = self.second_moment_grads[name] = grad * grad
            else:
                buf = self.second_moment_grads[name]
                buf.mul_(second_moment).add_(grad * grad, alpha=(1.0-dampening))
            return buf
        else:
            return grad * grad

    def compute_importance_scores(self):
        global_start_idx = 0
        self.global_scores = list() # Accumulate global scores
        # Calculate raw importance scores by varying criteria
        for group in self.param_groups:
            if group['is_prunable'] and not group['is_auxiliary']:
                calculate_importance_score_lhspg(self.importance_score_criteria, group, self.named_parameters)

        # Normalize importance_score
        # Calculate normalization_denoms
        normalization_denoms = dict.fromkeys(self.importance_score_criteria.keys(), self.safe_guard)
        for group in self.param_groups:
            if group['is_prunable'] and not group['is_auxiliary']:
                for proxy_name in self.importance_score_criteria:
                    normalization_denoms[proxy_name] += torch.sum(group['importance_scores'][proxy_name] ** 2, dim=0).item()
        for proxy_name in normalization_denoms:
            normalization_denoms[proxy_name] = np.sqrt(normalization_denoms[proxy_name]) + self.safe_guard

        self.cluster_importance_scores = dict()
        for cluster_name in self.prunable_param_group_clusters:
            param_group_cluster = self.prunable_param_group_clusters[cluster_name]
            global_start_idx = 0
            cluster_importance_score = list()
            for group in param_group_cluster:
                if group['is_prunable'] and not group['is_auxiliary']:
                    group['importance_scores']['overall'] = None
                    for proxy_name in self.importance_score_criteria:
                        if not proxy_name in group['importance_scores']:
                            continue
                        group['importance_scores'][proxy_name].mul_(self.importance_score_criteria[proxy_name] / normalization_denoms[proxy_name])
                        if group['importance_scores']['overall'] is None:
                            group['importance_scores']['overall'] = group['importance_scores'][proxy_name].clone()
                        else:
                            group['importance_scores']['overall'] += group['importance_scores'][proxy_name]              
                    
                    group['global_start_idx'] = global_start_idx
                    group['global_idxes'] = np.arange(global_start_idx, global_start_idx+group['num_groups'])
                    global_start_idx += group['num_groups']
                    cluster_importance_score.append(group['importance_scores']['overall'])
            self.cluster_importance_scores[cluster_name] = cluster_importance_score

    def identify_redundant_groups(self):
        for cluster_name in self.prunable_param_group_clusters:
            if len(self.cluster_importance_scores[cluster_name]) == 0:
                continue
            cluster_importance_score = torch.cat(self.cluster_importance_scores[cluster_name], dim=0)
            active_num_redundant_groups = self.active_num_redundant_groups_by_clusters[cluster_name][self.curr_pruning_period]

            # Pick up the groups with the least K importance scores
            curr_K = len(self.pruned_group_idx_by_cluster[cluster_name]) + active_num_redundant_groups
            _, top_indices = torch.topk(-cluster_importance_score, curr_K)
            top_indices = top_indices.cpu().numpy()
            top_indices = np.setdiff1d(top_indices, self.pruned_group_idx_by_cluster[cluster_name])[:active_num_redundant_groups].tolist()
            self.pruned_group_idx_by_cluster[cluster_name].extend(top_indices)
            
            for group in self.prunable_param_group_clusters[cluster_name]:
                if group['is_prunable'] and not group['is_auxiliary']:
                    global_active_redundant_idx = np.intersect1d(top_indices, group['global_idxes'])
                    self.active_redundant_idxes[group['id']] = (global_active_redundant_idx - group['global_start_idx']).tolist()
                    # Refine important_idx by group_divisible
                    if group['num_groups'] < self.group_divisible:
                        self.active_redundant_idxes[group['id']] = list()
                        self.pruned_idxes[group['id']] = list()
                    else:
                        curr_num_important_groups = len(self.important_idxes[group['id']])
                        trial_num_important_groups = curr_num_important_groups - len(self.active_redundant_idxes[group['id']])
                        if trial_num_important_groups % self.group_divisible != 0 or trial_num_important_groups <= 0:
                            ratio = trial_num_important_groups // self.group_divisible + 1 # Add one will preserve more groups, otherwise will slim more.
                            refined_num_important_groups = None
                            if ratio <= 1 or trial_num_important_groups == 0:
                                refined_num_important_groups = max(int(self.group_divisible), 1)
                            else:
                                refined_num_important_groups = max(int(ratio * self.group_divisible), int(self.group_divisible))   
                            refined_num_important_groups = min(group['num_groups'], refined_num_important_groups)
                            refined_num_active_redundant_groups = group['num_groups'] - len(self.pruned_idxes[group['id']]) - refined_num_important_groups
                            self.target_num_redundant_groups += (refined_num_active_redundant_groups - len(self.active_redundant_idxes[group['id']]))
                            self.active_redundant_idxes[group['id']] = self.active_redundant_idxes[group['id']][:refined_num_active_redundant_groups]     
                    self.important_idxes[group['id']] = [i for i in self.important_idxes[group['id']] if (i not in self.active_redundant_idxes[group['id']] and i not in self.pruned_idxes[group['id']])]
                    group['active_redundant_bool'] = torch.zeros(group['num_groups'], dtype=torch.bool).cuda()
                    group['active_redundant_bool'][self.active_redundant_idxes[group['id']]] = True                                                          

    def compute_grad_variant(self):
        for i, group in enumerate(self.param_groups):
            is_adam = group['variant'] == 'adam' or group['variant'] == 'adamw'
            first_bias_correction = 1.0 - group['first_momentum'] ** self.num_steps if is_adam else None
            second_bias_correction = 1.0 - group['second_momentum'] ** self.num_steps if is_adam else None
            group['grad_variant'] = dict()
            for j, (p_name, p) in enumerate(zip(group['p_names'], group['params'])):
                if p.grad is None:
                    continue
                refined_grad_f = torch.clone(p.grad.data).detach()
                if group['weight_decay'] is not None and group['variant'] != 'adamw':
                    refined_grad_f += group['weight_decay'] * p.data
                if not is_adam:
                    if group['first_momentum'] > 0.0 or group['dampening'] > 0.0:
                        refined_grad_f = self.get_first_momentum_grad(f"grad_first_moment_buffer_group_{i}_param_{j}", 
                            group['first_momentum'], group['dampening'], refined_grad_f)
                    group['grad_variant'][p_name] = refined_grad_f
                else:
                    first_moment_grad = self.get_first_momentum_grad(f"grad_first_moment_buffer_group_{i}_param_{j}", 
                        group['first_momentum'], group['first_momentum'], refined_grad_f) 
                    second_moment_grad_sq = self.get_second_momentum_grad_square(f"grad_second_moment_buffer_group_{i}_param_{j}", 
                        group['second_momentum'], group['second_momentum'], refined_grad_f)

                    exp_avg_first_moment_grad = first_moment_grad / first_bias_correction
                    exp_avg_second_moment_grad_sq = second_moment_grad_sq / second_bias_correction
                    denom = exp_avg_second_moment_grad_sq.sqrt().add_(self.safe_guard)
                    group['grad_variant'][p_name] = exp_avg_first_moment_grad / denom

    def reach_target_group_sparsity(self):
        if self.curr_num_zero_groups < self.target_num_redundant_groups:
            return False
        else:
            return True

    def commit_redundant_idxes(self):
        for group in self.param_groups:
            if group['is_prunable'] and not group['is_auxiliary']:
                self.pruned_idxes[group['id']].extend(self.active_redundant_idxes[group['id']])
                self.active_redundant_idxes[group['id']] = list()
                self.important_idxes[group['id']] = [i for i in range(group['num_groups']) if i not in self.pruned_idxes[group['id']]]
                group['importance_scores'] = dict()

    def step(self):
        self.num_steps += 1

        # First pass to compute gradient variant via different criteria
        self.compute_grad_variant()

        # Partition groups into important and redundant groups  
        if self.num_steps >= self.start_pruning_step and not self.reach_target_group_sparsity() and \
            self.curr_pruning_period < self.pruning_periods:
            if (self.num_steps - self.start_pruning_step - 1) % self.pruning_period_duration == 0:
                self.commit_redundant_idxes()
                self.compute_importance_scores()
                self.identify_redundant_groups()
                self.curr_pruning_period += 1

        # Second pass to update variables    
        t = (self.num_steps - self.start_pruning_step) % self.pruning_period_duration
        for i, group in enumerate(self.param_groups):
            if not group['is_prunable'] or len(self.active_redundant_idxes[group['id']]) == 0:
                for p_name, p in zip(group['p_names'], group['params']):
                    if p_name not in group['grad_variant']:
                        continue
                    if group['weight_decay'] is not None and group['variant'] == 'adamw':
                        p.data.add_(group['weight_decay'] * p.data, alpha=-group['lr'])
                    p.data.add_(group['grad_variant'][p_name], alpha=-group['lr'])
            elif group['is_prunable'] and len(self.active_redundant_idxes[group['id']]) > 0:
                for (p_name, p, p_transform) in zip(group['p_names'], group['params'], group['p_transform']):
                    # if p_name not in group['grad_variant']:
                    #     continue
                    if 'lora_B' in p_name:
                        if group['weight_decay'] is not None and group['variant'] == 'adamw':
                            p.data.add_(group['weight_decay'] * p.data, alpha=-group['lr'])
                        p.data.add_(group['grad_variant'][p_name], alpha=-group['lr'])
                        original_weight_name = p_name.split('lora_B')[0] + 'weight'
                        original_bias_name = p_name.split('lora_B')[0] + 'bias'
                        original_weight = self.named_parameters[original_weight_name]
                        original_bias = None if original_bias_name not in self.named_parameters else self.named_parameters[original_bias_name]
                        active_redundant_bool = None
                        if p_transform == TensorTransform.MULTIHEAD_HEADDIM:
                            active_redundant_bool = tensor_transformation(group['active_redundant_bool'], TensorTransform.REVERSE_MULTIHEAD_HEADDIM, \
                                                                          num_groups=group['num_groups'], num_heads=group['num_heads'])
                        elif p_transform == TensorTransform.MULTIHEAD_NUMHEAD:
                            active_redundant_bool = tensor_transformation(group['active_redundant_bool'], TensorTransform.REVERSE_MULTIHEAD_NUMHEAD, \
                                                                          num_groups=group['num_groups'], head_dim=group['head_dim'])
                        else:
                            active_redundant_bool = group['active_redundant_bool']
                        p.data[active_redundant_bool] *= (self.pruning_period_duration - t - 1.0) / (self.pruning_period_duration - t)
                        original_weight.data[active_redundant_bool] *= (self.pruning_period_duration - t - 1.0) / (self.pruning_period_duration - t)
                        if original_bias is not None:
                            original_bias.data[active_redundant_bool] *= (self.pruning_period_duration - t - 1.0) / (self.pruning_period_duration - t)
                    if 'lora_embedding_B' in p_name:
                        if group['weight_decay'] is not None and group['variant'] == 'adamw':
                            p.data.add_(group['weight_decay'] * p.data, alpha=-group['lr'])
                        p.data.add_(group['grad_variant'][p_name], alpha=-group['lr'])
                        for (decay_p_name, decay_param, decay_p_transform) in zip(group['p_names'], group['params'], group['p_transform']):    
                            if decay_p_transform == TensorTransform.BASIC:
                                decay_param.data[group['active_redundant_bool']] *= (self.pruning_period_duration - t - 1.0) / (self.pruning_period_duration - t)
                            elif decay_p_transform == TensorTransform.TRANSPOSE:
                                decay_param.data[:, group['active_redundant_bool']] *= (self.pruning_period_duration - t - 1.0) / (self.pruning_period_duration - t)
                            # print(decay_p_name, decay_param.shape, decay_p_transform)
                        break
                        
            if len(self.pruned_idxes[group['id']]) > 0:
                for p_name, p, p_transform in zip(group['p_names'], group['params'], group['p_transform']):
                    if 'lora_B' in p_name:
                        original_weight_name = p_name.split('lora_B')[0] + 'weight'
                        original_bias_name = p_name.split('lora_B')[0] + 'bias'
                        original_weight = self.named_parameters[original_weight_name]
                        original_bias = None if original_bias_name not in self.named_parameters else self.named_parameters[original_bias_name]
                        if p_transform == TensorTransform.MULTIHEAD_HEADDIM:
                            pruned_idxes = list()
                            for h in range(group['num_heads']):
                                pruned_idxes.extend([i + h * group['head_dim'] for i in self.pruned_idxes[group['id']]])
                            p.data[pruned_idxes] = 0.0
                            original_weight.data[pruned_idxes] = 0.0
                            if original_bias is not None:
                                original_bias.data[pruned_idxes] = 0.0
                        elif p_transform == TensorTransform.MULTIHEAD_NUMHEAD:
                            pruned_idxes = list()
                            for i in self.pruned_idxes[group['id']]:
                                pruned_idxes.extend([h + i * group['head_dim'] for h in range(group['head_dim'])])                            
                            p.data[pruned_idxes] = 0.0
                            original_weight.data[pruned_idxes] = 0.0
                            if original_bias is not None:
                                original_bias.data[pruned_idxes] = 0.0
                        elif p_transform == TensorTransform.TRANSPOSE and len(p.data.shape) > 1:
                            p.data[:, self.pruned_idxes[group['id']]] = 0.0
                            original_weight.data[self.pruned_idxes[group['id']]] = 0.0
                        else:
                            p.data[self.pruned_idxes[group['id']]] = 0.0
                            original_weight.data[self.pruned_idxes[group['id']]] = 0.0
                            if original_bias is not None:
                                original_bias.data[self.pruned_idxes[group['id']]] = 0.0

                    if 'lora_embedding_B' in p_name:
                        original_weight_name = p_name.split('lora_embedding_B')[0] + 'weight'
                        original_weight = self.named_parameters[original_weight_name]
                        p.data[self.pruned_idxes[group['id']]] = 0.0
                        original_weight.data[:, self.pruned_idxes[group['id']]] = 0.0

        if self.num_steps >= self.start_pruning_step and t == self.pruning_period_duration - 1:
            self.commit_redundant_idxes()

        self.curr_group_sparsity, _, self.curr_num_zero_groups = self.compute_group_sparsity_param_norm()
        return 

    def compute_group_sparsity_param_norm(self):
        total_num_zero_groups = 0
        norm_x = 0.0
        for group in self.param_groups:
            if group['is_prunable'] and not group['is_auxiliary']:
                norm_group = None
                for p_name, param, p_transform in zip(group['p_names'], group['params'], group['p_transform']):
                    if p_transform == TensorTransform.NO_PRUNE:
                        continue
                    param_transform = None
                    if p_transform == TensorTransform.MULTIHEAD_HEADDIM:
                        param_transform = tensor_transformation(param, p_transform, group['num_groups'], group['num_heads'])
                    else:
                        param_transform = tensor_transformation(param, p_transform, group['num_groups'])
                    if norm_group == None:
                        norm_group = torch.norm(param_transform, dim=1) ** 2
                    else:
                        norm_group += torch.norm(param_transform, dim=1) ** 2
                norm_group = torch.sqrt(norm_group)
                num_zero_groups = torch.sum(norm_group == 0).item()
                total_num_zero_groups += num_zero_groups
                norm_x += torch.sum(norm_group).item()
        group_sparsity = total_num_zero_groups / float(self.total_num_groups + self.safe_guard)
        return group_sparsity, norm_x, total_num_zero_groups
        
    def compute_norm_groups(self):
        self.norm_important_groups = 0.0
        self.norm_redundant_groups = 0.0
        self.num_important_groups = 0
        self.num_redundant_groups = 0
        
        for group in self.param_groups:
            if group['is_prunable'] and not group['is_auxiliary']:
                id = group['id']
                import_idxes = self.important_idxes[id]
                redund_idxes = self.pruned_idxes[id] + self.active_redundant_idxes[id]
                norm_group = None
                for p_name, param, p_transform in zip(group['p_names'], group['params'], group['p_transform']):
                    if p_transform == TensorTransform.NO_PRUNE:
                        continue
                    param_transform = None
                    if p_transform == TensorTransform.MULTIHEAD_HEADDIM:
                        param_transform = tensor_transformation(param, p_transform, group['num_groups'], group['num_heads'])
                    else:
                        param_transform = tensor_transformation(param, p_transform, group['num_groups'])
                    if norm_group == None:
                        norm_group = torch.norm(param_transform, dim=1) ** 2
                    else:
                        norm_group += torch.norm(param_transform, dim=1) ** 2
                norm_group = torch.sqrt(norm_group)
                self.norm_important_groups += torch.sum(norm_group[import_idxes]).item()
                self.norm_redundant_groups += torch.sum(norm_group[redund_idxes]).item()
                self.num_important_groups += len(import_idxes)
                self.num_redundant_groups += len(redund_idxes)

        return self.norm_important_groups, self.norm_redundant_groups, self.num_important_groups, self.num_redundant_groups  
                  
    def set_learning_rate(self, lr):
        for param_group in self.param_groups:
            param_group['lr'] = lr

    def get_learning_rate(self):
        for param_group in self.param_groups:
            lr = param_group['lr']
        return lr