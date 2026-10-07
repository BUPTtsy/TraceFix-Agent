import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import fs from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const expectedSteps=['initial_unchecked','complete','reload_completed','cancel_completion','reload_uncompleted','todo_filter','done_filter'];

function start(argv,cwd,env){
 const child=spawn(process.execPath,argv,{cwd,env,stdio:['ignore','pipe','pipe'],windowsHide:true});
 let stdout='',stderr='';
 child.stdout.on('data',chunk=>{stdout=(stdout+chunk.toString()).slice(-16000);});
 child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString()).slice(-4000);});
 const completed=new Promise((resolve,reject)=>{
  child.once('error',reject);
  child.once('close',(code,signal)=>resolve({code,signal,stdout,stderr}));
 });
 completed.catch(()=>{});
 return {child,completed};
}

async function stop(process){
 if(!process)return;
 if(process.child.exitCode===null && process.child.signalCode===null)process.child.kill();
 await process.completed;
}

async function execute(argv,cwd,env){
 const process=start(argv,cwd,env);
 let timer;
 try{
  return await Promise.race([
   process.completed,
   new Promise((resolve,reject)=>{timer=setTimeout(()=>reject(new Error('child process timed out')),60000);}),
  ]);
 }finally{
  clearTimeout(timer);
  await stop(process);
 }
}

async function availablePort(){
 const reservation=net.createServer();
 reservation.listen(0,'127.0.0.1');
 await once(reservation,'listening');
 const port=reservation.address().port;
 await new Promise((resolve,reject)=>reservation.close(error=>error?reject(error):resolve()));
 return port;
}

async function waitForServer(url,process){
 const deadline=Date.now()+15000;
 while(Date.now()<deadline){
  if(process.child.exitCode!==null || process.child.signalCode!==null)throw new Error('isolated BugBoard server exited before readiness');
  try{
   const response=await fetch(url+'/health',{signal:AbortSignal.timeout(1000)});
   if(response.ok)return;
  }catch{}
  await delay(100);
 }
 throw new Error('isolated BugBoard server did not become ready');
}

async function fixture(source,workspace,variant){
 await fs.mkdir(workspace,{recursive:true});
 for(const entry of ['src','server','scripts','package.json','tsconfig.json']){
  await fs.cp(path.join(source,entry),path.join(workspace,entry),{
   recursive:true,
   filter:entryPath=>!path.basename(entryPath).startsWith('.env'),
  });
 }
 if(variant==='completion_only'){
  const appFile=path.join(workspace,'src/App.tsx');
  const sourceText=await fs.readFile(appFile,'utf8');
  const reversible="onChange={()=>action(()=>updateTask(task.id,{status:task.status==='Todo'?'Done':'Todo'}))}";
  assert.equal(sourceText.split(reversible).length,2,'historical mutation requires exactly one reversible checkbox handler');
  const completionOnly="onChange={()=>{if(task.status==='Todo')action(()=>updateTask(task.id,{status:'Done'}));}}";
  await fs.writeFile(appFile,sourceText.replace(reversible,completionOnly));
 }
}

async function verifyVariant(source,runRoot,variant){
 const workspace=path.join(runRoot,variant);
 const temporary=path.join(workspace,'tmp');
 await fixture(source,workspace,variant);
 await fs.mkdir(temporary,{recursive:true});
 const port=await availablePort();
 const url=`http://127.0.0.1:${port}`;
 const env={
  ...process.env,
  NODE_PATH:path.join(source,'node_modules'),
  PORT:String(port),
  BUGBOARD_DATA:path.join(workspace,'tasks.json'),
  BUGBOARD_URL:url,
  TRACEFIX_PLAYWRIGHT_MODULE:process.env.TRACEFIX_PLAYWRIGHT_MODULE || path.join(root,'.tracefix/playwright/node_modules/playwright'),
  TEMP:temporary,TMP:temporary,TMPDIR:temporary,
 };
 const build=await execute(['scripts/build.mjs'],workspace,env);
 assert.equal(build.code,0,`isolated build failed: ${build.stderr}`);
 let server;
 try{
  server=start(['server/index.mjs'],workspace,env);
  await waitForServer(url,server);
  const execution=await execute([path.join(root,'evals/oracle.mjs'),'B01'],workspace,env);
  const report=JSON.parse(execution.stdout.trim());
  assert.equal(report.final_scoring_only,true,'Oracle must remain final-scoring only');
  assert.equal(report.case_id,'B01');
  if(variant==='clean'){
   assert.equal(execution.code,0,`clean implementation failed: ${report.error}`);
   assert.equal(report.passed,true);
   assert.deepEqual(report.steps.map(step=>step.name),expectedSteps);
   assert.ok(report.steps.every(step=>step.passed));
  }else{
   assert.equal(execution.code,1,'historical invalid repair must fail');
   assert.equal(report.passed,false);
   assert.deepEqual(report.steps.slice(0,3).map(step=>({name:step.name,passed:step.passed})),expectedSteps.slice(0,3).map(name=>({name,passed:true})));
   assert.equal(report.steps.at(-1).name,'cancel_completion','infrastructure errors do not count as rejecting the invalid repair');
   assert.equal(report.steps.at(-1).passed,false);
  }
  return {variant,expected:variant==='clean'?'pass':'reject_cancel_completion',exit_code:execution.code,oracle:report};
 }finally{
  await stop(server);
 }
}

async function main(){
 if(process.argv.includes('--help')){
  console.log('node scripts/checks/verify_b01_behavior_ui.mjs\nBuilds isolated clean and completion-only BugBoard copies under .tracefix, runs the real Playwright B01 Oracle, and writes report.json. Uses existing dependencies only.');
  return;
 }
 assert.equal(process.argv.length,2,'unexpected arguments');
 const parent=path.join(root,'.tracefix');
 await fs.mkdir(parent,{recursive:true});
 const runRoot=await fs.mkdtemp(path.join(parent,'b01-behavior-'));
 const report={status:'failed',run_root:path.relative(root,runRoot),results:[]};
 try{
  for(const variant of ['clean','completion_only'])report.results.push(await verifyVariant(path.join(root,'bugboard/target'),runRoot,variant));
  report.status='passed';
 }catch(failure){
  report.error=failure.message;
  process.exitCode=1;
 }finally{
  await fs.writeFile(path.join(runRoot,'report.json'),JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify(report));
 }
}

await main();
