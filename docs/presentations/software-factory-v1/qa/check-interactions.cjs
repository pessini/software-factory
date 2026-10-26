const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require('/home/lpessini/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({executablePath:'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const results=[];
 try {
  for(const viewport of [{width:1366,height:768},{width:1920,height:1080},{width:3840,height:2160}]){
   const page=await browser.newPage({viewport,deviceScaleFactor:1,reducedMotion:'reduce'});
   await page.goto(pathToFileURL(path.join(root,'software-factory-v1.html')).href);
   await page.addStyleTag({content:'html{scroll-behavior:auto!important}'});
   await page.locator('#notes').click();
   const notesVisible=await page.locator('.speaker-note:visible').count();
   await page.locator('#notes').click();
   await page.locator('.screenshot-button').first().click();
   const lightboxOpen=await page.locator('dialog').evaluate(e=>e.open);
   await page.keyboard.press('Escape');
   const lightboxClosed=await page.locator('dialog').evaluate(e=>!e.open);
   await page.locator('#present').click();
   const navHidden=!(await page.locator('.chapter-nav').isVisible());
   await page.locator('#workflow').scrollIntoViewIfNeeded();
   await page.screenshot({path:path.join(__dirname,'render-final',`${viewport.width}x${viewport.height}`,'present-workflow.png')});
   const overflowing=await page.evaluate(()=>({horizontal:document.documentElement.scrollWidth>innerWidth+1,chapters:[...document.querySelectorAll('.chapter')].filter(e=>e.getBoundingClientRect().height>innerHeight+3).map(e=>({id:e.id,height:e.getBoundingClientRect().height}))}));
   await page.locator('#projeto-pessoal').evaluate(e=>e.scrollIntoView({behavior:'instant'}));
   await page.evaluate(()=>document.activeElement.blur());
   await page.keyboard.press('ArrowRight');
   await page.waitForFunction(()=>Math.abs(document.querySelector('#tendencia').getBoundingClientRect().top)<2);
   const nextWorks=true;
   await page.evaluate(()=>document.activeElement.blur());
   await page.keyboard.press('Escape');
   const presentClosed=!(await page.locator('body').evaluate(e=>e.classList.contains('presenting')));
   results.push({viewport,notesVisible,lightboxOpen,lightboxClosed,navHidden,nextWorks,presentClosed,overflowing});
   await page.close();
  }
  fs.writeFileSync(path.join(__dirname,'interactions.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results,null,2));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
