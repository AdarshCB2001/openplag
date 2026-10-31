// Renders animation.html to an MP4 (needs Node, Playwright and ffmpeg).
// Usage: node render.js [output.mp4]
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
const HTML = 'file://' + path.join(__dirname, 'animation.html');
const OUT = process.argv[2] || path.join(__dirname, 'OpenPlag-demo-silent.mp4');
(async()=>{
  const FPS=30;
  const b=await chromium.launch(); const p=await b.newPage({viewport:{width:1920,height:1080}});
  await p.goto(HTML + '#0'); await p.waitForTimeout(400);
  const D=await p.evaluate(()=>DURATION); const N=Math.round(D*FPS);
  const ff=spawn('ffmpeg',['-y','-loglevel','error','-f','image2pipe','-framerate',String(FPS),'-c:v','png','-i','-',
    '-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',OUT]);
  ff.stderr.on('data',d=>process.stderr.write(d));
  const t0=Date.now();
  for(let i=0;i<N;i++){
    await p.evaluate(t=>render(t), i/FPS);
    const buf=await p.screenshot({type:'png'});
    if(!ff.stdin.write(buf)) await new Promise(r=>ff.stdin.once('drain',r));
    if(i%300===0) console.log(`frame ${i}/${N} ${((Date.now()-t0)/1000).toFixed(0)}s`);
  }
  ff.stdin.end(); await new Promise(r=>ff.on('close',r)); await b.close();
  console.log('done', ((Date.now()-t0)/1000).toFixed(0)+'s');
})();
