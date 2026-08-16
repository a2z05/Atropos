#!/usr/bin/env node
// Quick syntax check for dashboard inline JS
const fs = require('fs');
const path = __dirname + '/../dashboard/index.html';
const html = fs.readFileSync(path, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('No <script> block found');
  process.exit(1);
}
const js = m[1];
try {
  new Function(js);
  console.log('JS syntax OK (' + js.split('\n').length + ' lines)');
} catch (e) {
  console.error('JS SYNTAX ERROR:', e.message);
  process.exit(1);
}

// Same syntax check for the mobile chat page
const chatPath = __dirname + '/../dashboard/chat.html';
const chatHtml = fs.readFileSync(chatPath, 'utf8');
const cm = chatHtml.match(/<script>([\s\S]*?)<\/script>/);
if (!cm) {
  console.error('chat.html: No <script> block found');
  process.exit(1);
}
try {
  new Function(cm[1]);
  console.log('chat.html JS syntax OK (' + cm[1].split('\n').length + ' lines)');
} catch (e) {
  console.error('chat.html JS SYNTAX ERROR:', e.message);
  process.exit(1);
}

// Check all panel IDs match nav items
const panels = [...html.matchAll(/id="panel-(\w+)"/g)].map(m => m[1]);
const navPanels = [...html.matchAll(/data-panel="(\w+)"/g)].map(m => m[1]);
console.log('Panels in HTML:', panels.length, panels.join(', '));
console.log('Nav items:', navPanels.length, navPanels.join(', '));
const missing = panels.filter(p => !navPanels.includes(p));
if (missing.length) console.error('Panels without nav:', missing);
const extra = navPanels.filter(p => !panels.includes(p));
if (extra.length) console.error('Nav items without panel:', extra);
if (!missing.length && !extra.length) console.log('All panels have nav items ✓');

// Check PANELS array
const panelArrMatch = js.match(/const PANELS\s*=\s*\[([^\]]+)\]/);
if (panelArrMatch) {
  const arrPanels = panelArrMatch[1].replace(/['"]/g,'').split(',').map(s=>s.trim());
  console.log('PANELS array:', arrPanels.length, arrPanels.join(', '));
  const missingInArray = panels.filter(p => !arrPanels.includes(p));
  if (missingInArray.length) console.error('Panels missing from PANELS array:', missingInArray);
  else console.log('All panels in PANELS array ✓');
}
