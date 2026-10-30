from a3_dual_arm_sim.workflows.policy_adapters import ExpertPolicyAdapter
from a3_dual_arm_sim.controllers.same_column_batch_expert import A3SameColumnBatchExpert

class DampedExpert(ExpertPolicyAdapter):
    def __init__(self):
        super().__init__(A3SameColumnBatchExpert, name='damped_same_column')

    def act(self, observation, task=''):
        action = super().act(observation, task).copy()
        action[:6] *= 0.9
        action[7:13] *= 0.9
        return action

def make_policy():
    return DampedExpert()
