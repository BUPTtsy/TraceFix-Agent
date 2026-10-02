// Host-owned one-step training oracle; local disposable BugBoard only.
import {createRequire} from 'node:module';import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || 'playwright');
let raw='';for await(const c of process.stdin)raw+=c;
const input=JSON.parse(raw),url=input.url;
if(!['127.0.0.1','localhost','app'].includes(new URL(url).hostname))throw new Error('训练地址必须属于本地沙箱');
const browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1280,height:800},locale:'en-US'});const page=await context.newPage();
const result={oracle_success:false,invalid_action:false,action_cost:1,potential_before:0,potential_after:0};
async function act(a){
 const locator=a.locator?page.getByRole(a.locator.role,{name:a.locator.name,exact:true}):null;
 if(a.kind==='navigate'){if(new URL(a.value).origin!==new URL(url).origin)throw Object.assign(new Error('禁止导航到训练沙箱之外的地址'),{code:'safety'});await page.goto(a.value);}
 else if(a.kind==='click')await locator.click({timeout:3000});
 else if(a.kind==='type')await locator.fill(a.value,{timeout:3000});
 else if(a.kind==='select')await locator.selectOption(a.value,{timeout:3000});
 else if(a.kind==='press'){if(!['Tab','Enter','Escape','ArrowDown','ArrowUp','Space'].includes(a.value))throw Object.assign(new Error('训练动作包含未获允许的按键'),{code:'safety'});await page.keyboard.press(a.value);}
 else if(!['observe','finish'].includes(a.kind))throw new Error(`训练动作类型无效：${a.kind}`);
}
async function checks(){let passed=0;for(const a of input.assertions){const l=page.getByRole(a.locator.role,{name:a.locator.name,exact:true});let ok=false;try{if(a.condition==='absent')ok=await l.count()===0;else if(a.condition==='checked')ok=await l.isChecked();else if(a.condition==='disabled')ok=await l.isDisabled();else if(a.condition==='enabled')ok=await l.isEnabled();else ok=await l.isVisible();}catch{}if(ok)passed++;}return passed/input.assertions.length;}
try{
 const reset=await context.request.post(url+'/__reset');if(!reset.ok())throw new Error(`训练环境重置失败（HTTP ${reset.status()}）`);
 await page.goto(url);await page.getByRole('heading',{name:'Task board',exact:true}).waitFor();
 for(const a of input.prefix || [])await act(a);
 await page.waitForTimeout(200);
 const tasks=await (await context.request.get(url+'/api/tasks')).json();
 const storage=await context.storageState();
 const dom=await page.locator('body').ariaSnapshot();
 result.state_fingerprint=crypto.createHash('sha256').update(JSON.stringify({tasks,storage,dom,url:page.url()})).digest('hex');
 result.potential_before=await checks();
 try{await act(input.action);}catch(e){result.invalid_action=true;result.safety_violation=e.code==='safety';result.message=`训练动作执行失败：${e.message}`;}
 await page.waitForTimeout(250);
 result.potential_after=await checks();result.oracle_success=result.potential_after===1;
}finally{await browser.close();}
console.log(JSON.stringify(result));
