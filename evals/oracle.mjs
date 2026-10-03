// Independent evaluator. Never mounted in the Agent workspace. No golden patches
// are provided to the Patcher. URL must name an authorized disposable sandbox.
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || 'playwright');
const url=process.env.BUGBOARD_URL || 'http://127.0.0.1:3000';
const caseId=process.argv[2] || 'B01';
if(!['127.0.0.1','localhost','app'].includes(new URL(url).hostname))throw new Error('Oracle restricted to local sandbox');
let browser,context,page;
let passed=false,error=null;
const steps=[];
const by=(role,name)=>page.getByRole(role,{name,exact:true});
const visible=async(role,name)=>{await by(role,name).waitFor({state:'visible',timeout:4000});};
const ensure=(ok,message)=>{if(!ok)throw new Error(message);};
async function step(name,work){
 try{await work();steps.push({name,passed:true});}
 catch(failure){steps.push({name,passed:false,error:failure.message});throw failure;}
}
async function checkboxState(checked){
 const checkbox=by('checkbox','Complete Write project brief');
 await checkbox.waitFor({state:'visible',timeout:4000});
 const deadline=Date.now()+4000;
 while(Date.now()<deadline){
  if(await checkbox.isEnabled() && await checkbox.isChecked()===checked){
   ensure(await page.getByRole('alert').count()===0,'task update displayed an error');
   return;
  }
  await page.waitForTimeout(50);
 }
 throw new Error(checked?'completed state missing':'ability to cancel completion missing');
}
async function toggleCompletion(checked){
 const [response]=await Promise.all([
  page.waitForResponse(response=>/\/api\/tasks\/\d+$/.test(new URL(response.url()).pathname) && ['PATCH','POST'].includes(response.request().method()),{timeout:4000}),
  by('checkbox','Complete Write project brief').click(),
 ]);
 ensure(response.ok(),'task update failed');
 await response.finished();
 await checkboxState(checked);
}
try{
 browser=await chromium.launch({headless:true});
 context=await browser.newContext({viewport:{width:1280,height:800},locale:'en-US'});
 page=await context.newPage();
 const reset=await context.request.post(url+'/__reset');ensure(reset.ok(),'reset failed');
 await page.goto(url);await visible('heading','Task board');
 await by('checkbox','Complete Write project brief').waitFor({timeout:5000});
 switch(caseId){
 case 'B01':
  await step('initial_unchecked',()=>checkboxState(false));
  await step('complete',()=>toggleCompletion(true));
  await step('reload_completed',async()=>{await page.reload();await checkboxState(true);});
  await step('cancel_completion',()=>toggleCompletion(false));
  await step('reload_uncompleted',async()=>{await page.reload();await checkboxState(false);});
  await step('todo_filter',async()=>{await by('button','Todo').click();await checkboxState(false);});
  await step('done_filter',async()=>{
   await by('button','Done').click();
   await visible('checkbox','Complete Prepare release checklist');
   ensure(await by('checkbox','Complete Write project brief').count()===0,'uncompleted task remains in Done');
  });
  break;
 case 'B02': await by('button','Delete Write project brief').click();await page.waitForTimeout(500);await page.reload();ensure(await by('checkbox','Complete Write project brief').count()===0,'delete failed');break;
 case 'B03': await by('button','Add task').click();await visible('alert','');ensure((await page.getByRole('alert').innerText()).includes('required'),'missing error');break;
 case 'B04': await by('button','Edit Write project brief').click();ensure(await by('textbox','Task title').inputValue()==='Write project brief','draft missing');break;
 case 'B05': await by('button','Todo').click();await visible('checkbox','Complete Write project brief');break;
 case 'B06': await by('textbox','Search tasks').fill('WRITE');await visible('checkbox','Complete Write project brief');break;
 case 'B07': await by('textbox','New task title').fill('Fresh item');await by('button','Add task').click();await visible('checkbox','Complete Fresh item');break;
 case 'B08': await visible('checkbox','Complete Write project brief');break;
 case 'B09': await by('textbox','New task title').fill('Fresh item');await by('button','Add task').click();await page.waitForTimeout(600);ensure(await by('button','Add task').isEnabled(),'busy state stuck');break;
 case 'B10': await by('button','Edit Write project brief').click();await by('button','Cancel').click();ensure(!(await page.getByRole('dialog').isVisible()),'dialog remains open');break;
 case 'B11': await by('button','Edit Write project brief').click();await by('textbox','Task title').fill('Revised brief');await by('button','Save changes').click();await page.waitForTimeout(500);await page.reload();await visible('checkbox','Complete Revised brief');break;
 case 'B12': await by('button','Done').click();await visible('checkbox','Complete Prepare release checklist');ensure(await by('checkbox','Complete Write project brief').count()===0,'wrong status included');break;
 case 'CLEAN': await visible('button','Add task');break;
 default: throw new Error('Unknown case');
 }
 passed=true;
}catch(e){error=e.message;}
finally{if(browser)await browser.close();}
console.log(JSON.stringify({case_id:caseId,passed,error,steps,oracle:'independent-playwright-v2',final_scoring_only:true}));
process.exitCode=passed?0:1;
