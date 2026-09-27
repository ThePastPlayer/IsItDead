const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');
const {window} = parseHTML('<html><body></body></html>');
vm.runInNewContext(fs.readFileSync('custom_components/is_it_dead/frontend/is_it_dead_panel.js','utf8'), {HTMLElement:window.HTMLElement, customElements:window.customElements, document:window.document, console, setTimeout, clearTimeout});
const make = () => new (window.customElements.get('is-it-dead-panel'))();
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

