"""Online K=4 sampling with environment-verified one-step rewards and GRPO update."""
import json
import subprocess
from pathlib import Path

from dataset import load
from reward import group_rewards
from tracefix.messages import ChineseArgumentParser
from tracefix.runtime.contracts import BrowserAction


def main():
    p=ChineseArgumentParser(description='执行环境验证的单步 GRPO 训练，每组采样 4 个候选动作');p.add_argument('--dataset',type=Path,required=True,help='JSONL 数据集路径')
    p.add_argument('--model',default='Qwen/Qwen2.5-VL-3B-Instruct',help='基础模型名称或路径');p.add_argument('--revision',required=True,help='不可变的模型提交版本')
    p.add_argument('--sft-adapter',required=True,help='已完成 SFT 训练的适配器路径');p.add_argument('--output',required=True,help='模型与实验记录的输出目录')
    p.add_argument('--steps',type=int,default=10,help='训练步数，默认为 10');p.add_argument('--url',default='http://127.0.0.1:3000',help='本地 BugBoard 训练沙箱地址')
    args=p.parse_args()
    if args.revision in {'main','latest'}:raise ValueError('请固定不可变的模型提交版本，不能使用 main 或 latest')
    import torch
    from PIL import Image
    from datasets import Dataset
    from transformers import AutoProcessor,Qwen2_5_VLForConditionalGeneration
    from peft import PeftModel
    from trl import GRPOConfig,GRPOTrainer
    if not torch.cuda.is_available():raise RuntimeError('GPU 不可用，未执行 GRPO 参数更新')
    rows=load(args.dataset)
    examples=[]
    for r in rows:
        if 'prefix' not in r or 'assertions' not in r:raise ValueError('强化学习样本必须包含用于回放的动作前缀和独立断言')
        examples.append({'prompt':[{'role':'user','content':[{'type':'image'},{'type':'text','text':json.dumps({'goal':r['goal'],'observation':r['observation']})+' Return canonical BrowserAction JSON.'}]}],
            'image':Image.open(r['image']).convert('RGB'),'prefix':r['prefix'],'assertions':r['assertions']})
    model=Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model,revision=args.revision,torch_dtype=torch.bfloat16,attn_implementation='sdpa')
    model=PeftModel.from_pretrained(model,args.sft_adapter,is_trainable=True)
    processor=AutoProcessor.from_pretrained(args.model,revision=args.revision)
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    def env_reward(completions,prefix,assertions,**kwargs):
        rewards=[]
        for i in range(0,len(completions),4):
            results=[]
            for j in range(i,i+4):
                content=completions[j][0]['content'] if isinstance(completions[j],list) else completions[j]
                try:action=BrowserAction.model_validate_json(content).model_dump()
                except ValueError:action={'kind':'invalid'}
                data={'url':args.url,'prefix':prefix[j],'assertions':assertions[j],'action':action}
                try:
                    proc=subprocess.run(['node',str(Path(__file__).with_name('step_env.mjs'))],input=json.dumps(data),text=True,encoding='utf-8',capture_output=True,timeout=60,check=True)
                except subprocess.CalledProcessError as error:
                    detail='\n'.join(part.strip() for part in (error.stdout,error.stderr) if part and part.strip())
                    raise RuntimeError(f'训练环境执行失败（退出码 {error.returncode}）'
                        + (f'\n训练环境原始输出：\n{detail}' if detail else '')) from error
                results.append(json.loads(proc.stdout))
            values,status=group_rewards(results)
            with (output/'environment_rewards.jsonl').open('a') as f:f.write(json.dumps({'results':results,'rewards':values,'status':status})+'\n')
            rewards.extend(values)
        return rewards
    config=GRPOConfig(output_dir=args.output,max_steps=args.steps,num_generations=4,
        per_device_train_batch_size=4,gradient_accumulation_steps=1,max_completion_length=256,
        max_prompt_length=None,bf16=True,learning_rate=5e-6,beta=0.0,logging_steps=1,
        save_steps=5,report_to='none',remove_unused_columns=False,use_vllm=False)
    trainer=GRPOTrainer(model=model,args=config,reward_funcs=env_reward,
        train_dataset=Dataset.from_list(examples),processing_class=processor)
    result=trainer.train();trainer.save_model(args.output);processor.save_pretrained(args.output)
    (output/'experiment.json').write_text(json.dumps({'kind':'environment-verified single-step GRPO','metrics':result.metrics,'model_revision':args.revision,'samples':len(rows),'peak_vram_bytes':torch.cuda.max_memory_allocated()},indent=2))

if __name__=='__main__':main()
