const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const source=fs.readFileSync('src/beings_leads/dashboard/app.js','utf8');
const a=source.indexOf('  const enabledSteps = steps.filter');
const b=source.indexOf('  // Keep the orb',a);
const render=new Function('steps','status','scan','active','directoryProgress','escapeHtml','humanize','$','scanFinished','idle','initializing',source.slice(a,b)+';return phaseMarkup;');
const steps=Array.from({length:9},(_,i)=>({phase:{id:'step'+i,label:'Step '+i},number:i+1,off:i>3,finished:false,current:false}));
const html=render(steps,'idle',{},false,'',x=>x,x=>x,()=>({dataset:{past:'true',upcoming:'true'}}),false,true);
assert.equal((html.match(/<li /g)||[]).length,4);
assert(!html.includes('data-phase-group'));
assert(!html.includes('phase-upcoming'));
assert(!html.includes('Step 4'));
const runningTimeline = (enabled, current) => render(
  steps.map(step => ({...step, off:!enabled.includes(step.number),
    finished:step.number < current,current:step.number === current})),
  'running',{},true,'',x=>x,x=>x,()=>({dataset:{}}),false,false,false,
);
const singletons = runningTimeline([1,3,5,7,9],5);
assert.doesNotMatch(singletons,/data-phase-group|phase-past|phase-upcoming/);
assert.match(singletons,/Step 0.*Finished/);
assert.match(singletons,/Step 8.*Waiting/);
const groups = runningTimeline([1,2,3,5,7,8,9],5);
assert.equal((groups.match(/\+2 steps/g)||[]).length,2);
assert.doesNotMatch(groups,/\+1 steps/);
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const page=await browser.newPage();
  await page.setContent('<style>'+fs.readFileSync('src/beings_leads/dashboard/styles.css','utf8')+'</style><ol class="smart-scan-phases">'+html+'</ol>');
  const edges=await page.locator('ol').evaluate(el=>({top:getComputedStyle(el.firstElementChild,'::before').display,bottom:getComputedStyle(el.lastElementChild,'::after').content}));
  assert.equal(edges.top,'none');assert(['none','normal'].includes(edges.bottom));
  console.log('Cleared timeline shows all enabled steps without collapse controls or dangling end connectors.');
 }finally{await browser.close();}
})();
