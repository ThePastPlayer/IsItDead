const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');
const {window} = parseHTML('<html><body></body></html>');
vm.runInNewContext(fs.readFileSync('custom_components/is_it_dead/frontend/is_it_dead_panel.js','utf8'), {HTMLElement:window.HTMLElement, customElements:window.customElements, document:window.document, console, setTimeout, clearTimeout, setInterval, clearInterval});
const make = () => new (window.customElements.get('is-it-dead-panel-v1-3-1'))();
const state = (id, status='alive') => ({entity_id:'binary_sensor.'+id, state:'off', attributes:{device_name:id, tracked_device_id:id, health_status:status, entity_count:1, entities:['sensor.'+id], entity_details:[{entity_id:'sensor.'+id,state:'20'}]}});
test('expanded entities preserve DOM identity across reports and sorting', () => {
 const p=make(); p.hass={states:{"binary_sensor.a":state('a'),"binary_sensor.b":state('b')}};
 p._expandedDevices.add('binary_sensor.a'); p.render();
 const card=p.shadowRoot.querySelector('[data-render-key="binary_sensor.a"]');
 assert.ok(card);
 const wrapper=card.querySelector('.entity-details-wrapper');
 assert.ok(wrapper);
 for(let i=0;i<5;i++) p.hass={states:{"binary_sensor.a":state('a'),"binary_sensor.b":state('b','dead')}};
 assert.equal(p.shadowRoot.querySelector('[data-render-key="binary_sensor.a"]'),card);
 assert.equal(card.querySelector('.entity-details-wrapper'),wrapper);
 assert.match(wrapper.getAttribute('style'),/max-height: none/);
 p._expandedDevices.delete('binary_sensor.a'); p.render();
 assert.match(wrapper.getAttribute('style'),/max-height: 0/);
});

const click = el => el.dispatchEvent(new window.Event('click', {bubbles:true}));
test('real expand clicks stay open through repeated hass updates and close once', () => {
 const p=make(); const data={states:{'binary_sensor.a':state('a')}};p.hass=data;
 click(p.shadowRoot.querySelector('.expand-toggle span'));
 const wrapper=p.shadowRoot.querySelector('.entity-details-wrapper');
 for(let i=0;i<20;i++){p.hass=data;assert.match(wrapper.getAttribute('style'),/none/);}
 click(p.shadowRoot.querySelector('.expand-toggle span'));
 for(let i=0;i<10;i++){p.hass=data;assert.match(wrapper.getAttribute('style'),/max-height: 0/);}
 assert.equal(p.shadowRoot.querySelector('.entity-details-wrapper'),wrapper);
});
test('guided review needs explicit start, preserves disabled automations and renders progress', async () => {
 const p=make();const calls=[];
 const preview={devices:[{device_id:'a',name:'Door',native_radio:true,needs_wake:true}],automations:[{entity_id:'automation.one',name:'Light',related:true,state:'on'},{entity_id:'automation.two',name:'Already off',related:true,state:'off'}],session:{}};
 p.hass={states:{'binary_sensor.a':state('a')},callWS:async req=>{calls.push(req);return {response:req.service==='test_preview'?preview:{active:true,phase:'active',expires_at:Date.now()/1000+1800,restore:['automation.one'],devices:{a:{device_id:'a',name:'Door',status:'observed',evidence:'radio'}}}};}};
 await p._openTest();
 assert.deepEqual(calls.map(c=>c.service),['test_preview']);
 await p._testAction({target:p.shadowRoot.querySelector('[data-test-action="review"]')});
 assert.equal(p.shadowRoot.querySelector('[data-test-automation="automation.two"]').disabled,true);
 await p._testAction({target:p.shadowRoot.querySelector('[data-test-action="start"]')});
 assert.deepEqual(Array.from(calls.at(-1).service_data.automation_ids),['automation.one']);
 assert.match(p.shadowRoot.querySelector('#guided-test').textContent,/Contact radio reçu/);
 const row=p.shadowRoot.querySelector('[data-render-key="test:a"]');assert.ok(row.classList.contains('test-done'));p._renderTest();
 assert.equal(p.shadowRoot.querySelector('[data-render-key="test:a"]'),row);
 await p._testAction({target:p.shadowRoot.querySelector('[data-test-action="close"]')});
 assert.equal(calls.some(c=>c.service==='end_test'),false);
 p.disconnectedCallback();
});
test('Zigbee probe button and generic test button reflect backend capability',()=>{
 const p=make();const a=state('a');a.attributes.zigbee_evidence={backend:'zha'};
 p.hass={states:{'binary_sensor.a':a,'binary_sensor.b':state('b')}};
 assert.ok(p.shadowRoot.querySelector('[data-render-key="binary_sensor.a"] [data-action="check"]'));
 assert.equal(p.shadowRoot.querySelector('[data-render-key="binary_sensor.b"] [data-action="check"]'),null);
 assert.equal(p.shadowRoot.querySelectorAll('[data-action="manual-test"]').length,2);
});

test('HA cached panel names all mount and receive properties before attachment', () => {
 for(const name of ['is-it-dead-panel','is-it-dead-panel-v1-3-0','is-it-dead-panel-v1-3-1','is-it-dead-panel-v1-3-2']) {
  const p=window.document.createElement(name);
  // Match Home Assistant: create by config.name, set props, then append.
  Object.assign(p,{panel:{config:{_panel_custom:{name}}},hass:{states:{'binary_sensor.a':state('a')}},narrow:false,route:{path:''}});
  window.document.body.appendChild(p);
  assert.match(p.shadowRoot.querySelector('h1').textContent,/1\.3\.2/);
  assert.equal(p.shadowRoot.querySelectorAll('.device-card').length,1);
  p.remove();
 }
});
test('loading the module via another URL does not re-register existing elements', () => {
 const source=fs.readFileSync('custom_components/is_it_dead/frontend/is_it_dead_panel.js','utf8');
 assert.doesNotThrow(()=>vm.runInNewContext(source,{HTMLElement:window.HTMLElement,customElements:window.customElements,document:window.document,console,setTimeout,clearTimeout,setInterval,clearInterval}));
});
