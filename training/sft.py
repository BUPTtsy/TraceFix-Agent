"""Qwen2.5-VL LoRA SFT, completion-only labels, no image-token truncation."""
import json
from pathlib import Path

from dataset import load
from tracefix.messages import ChineseArgumentParser


def main():
    p=ChineseArgumentParser(description='执行 Qwen2.5-VL LoRA 监督微调（SFT）');p.add_argument('--dataset',type=Path,required=True,help='JSONL 数据集路径')
    p.add_argument('--model',default='Qwen/Qwen2.5-VL-3B-Instruct',help='基础模型名称或路径');p.add_argument('--revision',required=True,help='不可变的模型提交版本')
    p.add_argument('--output',required=True,help='模型与实验记录的输出目录');p.add_argument('--steps',type=int,default=20,help='训练步数，默认为 20')
    p.add_argument('--smoke',action='store_true',help='执行一次真实参数更新，需要模型权重和 GPU')
    args=p.parse_args()
    if args.revision in {'main','latest'}:raise ValueError('请使用不可变的模型提交版本，不能使用 main 或 latest')
    import torch
    from PIL import Image
    from datasets import Dataset
    from transformers import AutoProcessor,Qwen2_5_VLForConditionalGeneration
    from peft import LoraConfig
    from trl import SFTConfig,SFTTrainer
    if not torch.cuda.is_available():raise RuntimeError('GPU 不可用，未执行训练')
    processor=AutoProcessor.from_pretrained(args.model,revision=args.revision,trust_remote_code=False)
    processor.tokenizer.padding_side='right'
    rows=load(args.dataset)
    def collate(batch):
        prompts,full,images=[],[],[]
        for row in batch:
            image=Image.open(row['image']).convert('RGB');images.append(image)
            text=json.dumps({'goal':row['goal'],'observation':row['observation'],'allowed_actions':row['allowed_actions']},ensure_ascii=False)
            prompt=[{'role':'user','content':[{'type':'image'},{'type':'text','text':text+'\nReturn a canonical BrowserAction JSON.'}]}]
            response={'role':'assistant','content':[{'type':'text','text':json.dumps(row['action'],ensure_ascii=False)}]}
            prompts.append(processor.apply_chat_template(prompt,tokenize=False,add_generation_prompt=True))
            full.append(processor.apply_chat_template(prompt+[response],tokenize=False,add_generation_prompt=False))
        encoded=processor(text=full,images=images,padding=True,return_tensors='pt')
        prefix=processor(text=prompts,images=images,padding=True,return_tensors='pt')
        labels=encoded['input_ids'].clone()
        for i in range(len(batch)):
            n=int(prefix['attention_mask'][i].sum())
            if not torch.equal(encoded['input_ids'][i,:n],prefix['input_ids'][i,:n]):
                raise ValueError('多模态模板前缀不匹配，不能使用错误的掩码进行训练')
            labels[i,:n]=-100
        labels[encoded['attention_mask']==0]=-100
        if not (labels!=-100).any():raise ValueError('全部输出 token 均被掩码屏蔽，无法训练')
        encoded['labels']=labels
        return encoded
    model=Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model,revision=args.revision,
        torch_dtype=torch.bfloat16,attn_implementation='sdpa',trust_remote_code=False)
    config=SFTConfig(output_dir=args.output,max_steps=1 if args.smoke else args.steps,
        per_device_train_batch_size=1,gradient_accumulation_steps=4,learning_rate=2e-4,
        bf16=True,gradient_checkpointing=True,logging_steps=1,save_steps=10,report_to='none',
        max_length=None,remove_unused_columns=False,dataset_kwargs={'skip_prepare_dataset':True})
    trainer=SFTTrainer(model=model,args=config,train_dataset=Dataset.from_list(rows),
        processing_class=processor,data_collator=collate,
        peft_config=LoraConfig(r=16,lora_alpha=32,lora_dropout=0.05,target_modules=['q_proj','v_proj'],task_type='CAUSAL_LM'))
    collate(rows[:1])
    result=trainer.train()
    trainer.save_model(args.output);processor.save_pretrained(args.output)
    Path(args.output,'experiment.json').write_text(json.dumps({'model':args.model,'revision':args.revision,
        'samples':len(rows),'trained_node':'BrowserActionPolicy','metrics':result.metrics,
        'peak_vram_bytes':torch.cuda.max_memory_allocated(),'smoke':args.smoke},indent=2))

if __name__=='__main__':main()
