from tracefix.runtime.contracts import BrowserAction


def reward(result):
    if result.get('safety_violation'):return -1.0
    before=float(result['potential_before']);after=float(result['potential_after'])
    if not 0<=before<=1 or not 0<=after<=1:raise ValueError('动作前后的势函数值必须在 0 到 1 之间')
    return float(bool(result['oracle_success']))+0.2*(after-before)-0.3*bool(result['invalid_action'])-0.02*result.get('action_cost',1)


def group_rewards(results):
    if len(results)!=4:raise ValueError('每组必须包含 4 个候选结果（K=4）')
    if len({r['state_fingerprint'] for r in results})!=1:
        raise ValueError('同组候选必须从相同的服务端与浏览器状态开始')
    rewards=[reward(r) for r in results]
    if max(rewards)-min(rewards)<1e-9:
        return [0.0]*4,'zero_variance'
    return rewards,'valid'
