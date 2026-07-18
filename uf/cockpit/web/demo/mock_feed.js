/* ========================================================================
   demo/mock_feed.js — stands in for ui_reads_consumer's push.
   PREVIEW/HARNESS ONLY — never loaded by the cockpit. Shapes are exactly
   {...Reading.to_dict(), payload}. ORDER moved to cockpit.js; everything else
   (CANS, mockFeed, the state-toggle driver) stays here.
   ======================================================================== */
const now = () => Date.now()/1000;

// canned readings, one per state, per capability — the toggle swaps between them
const CANS = {
  bgp: {
    PRESENT: () => ({key:'bgp', state:'PRESENT', reason:'4 peers, 1 not Established', as_of:now(), age_s:2.1,
      frames:[{label:'peers not Established', value:1, ceiling:0, status:'CRIT'}],
      payload:[                                  /* list(peers.values()) + re-injected peerAddress */
        {peerAddress:'172.16.7.1', description:'spine2', peerState:'Established', prefixReceived:412},
        {peerAddress:'172.16.7.2', description:'leaf-1', peerState:'Established', prefixReceived:88},
        {peerAddress:'172.16.7.4', description:'leaf-2', peerState:'Active'},
        {peerAddress:'172.16.7.5', description:'border', peerState:'Established', prefixReceived:1204},
      ]}),
    ABSENT:  () => ({key:'bgp', state:'ABSENT', reason:'BGP not configured', as_of:now(), age_s:2.1, frames:[], payload:null}),
    UNREAD:  () => ({key:'bgp', state:'UNREAD', reason:'show ip bgp summary | json — parse failed', as_of:now(), age_s:2.1, frames:[], payload:null}),
  },
  ospf: {
    PRESENT: () => ({key:'ospf', state:'PRESENT', reason:'3 adjacencies, 1 not Full', as_of:now(), age_s:3.4,
      frames:[{label:'adjacencies not Full', value:1, ceiling:0, status:'CRIT'}],
      payload:[   /* CONVERGED contract list; extras demonstrate graceful dashes:
                     one fully-populated, one required-only, one down (no role) */
        {routerId:'10.0.0.1', adjacencyState:'full',  interfaceName:'Ethernet1',
         neighborAddress:'192.168.100.1', priority:128, drState:'DR',
         area:'0.0.0.0', upTime:'41d03h'},
        {routerId:'10.0.0.2', adjacencyState:'full',  interfaceName:'Ethernet2'},
        {routerId:'10.0.0.9', adjacencyState:'2-Way', interfaceName:'Ethernet9',
         neighborAddress:'192.168.100.9', priority:0},
      ]}),
    ABSENT:  () => ({key:'ospf', state:'ABSENT', reason:'routing protocol not configured', as_of:now(), age_s:3.4, frames:[], payload:null}),
    UNREAD:  () => ({key:'ospf', state:'UNREAD', reason:'show ip ospf neighbor | json — no answer', as_of:now(), age_s:3.4, frames:[], payload:null}),
  },
  lldp: {   // never ABSENT: empty neighbors -> UNREAD, not absence
    PRESENT: () => ({key:'lldp', state:'PRESENT', reason:'5 lldp neighbors observed', as_of:now(), age_s:4.2,
      frames:[],
      payload:[
        {port:'Ethernet1',   neighborDevice:'rtr-a.example.net', neighborPort:'Ethernet48',   ttl:120},
        {port:'Ethernet2',   neighborDevice:'rtr-b.example.net', neighborPort:'Ethernet48',   ttl:120},
        {port:'Ethernet3',   neighborDevice:'sw-c.example.net',  neighborPort:'Ethernet5',    ttl:120},
        {port:'Ethernet4',   neighborDevice:'sw-d.example.net',  neighborPort:'Ethernet52/1', ttl:120},
        {port:'Management1', neighborDevice:'oob-e.example.net', neighborPort:'Gi1/0/7',      ttl:120},
      ]}),
    UNREAD:  () => ({key:'lldp', state:'UNREAD', reason:'show lldp neighbors | json \u2014 no answer', as_of:now(), age_s:4.2, frames:[], payload:null}),
  },
  interfaces: {   // never ABSENT: a box always has interfaces; empty map -> UNREAD
    PRESENT: () => ({key:'interfaces', state:'PRESENT', reason:'6 interfaces: 2 up, 1 admin, 2 not-connected, 2 faulted', as_of:now(), age_s:3.0,
      frames:[{label:'interfaces faulted', value:2, ceiling:0, status:'CRIT'}],
      payload:{
        'Ethernet1':  {linkStatus:'connected',  lineProtocolStatus:'up',   description:'transit-a',   bandwidth:1e10, interfaceType:'10GBASE-LR'},
        'Ethernet2':  {linkStatus:'connected',  lineProtocolStatus:'up',   description:'core-uplink', bandwidth:1e11, interfaceType:'100GBASE-CWDM4'},
        'Ethernet4':  {linkStatus:'errdisabled',lineProtocolStatus:'down', description:'',            bandwidth:1e10, interfaceType:'10GBASE-SR'},
        'Ethernet5':  {linkStatus:'connected',  lineProtocolStatus:'down', description:'flapping',    bandwidth:1e10, interfaceType:'10GBASE-SR'},
        'Ethernet9':  {linkStatus:'notconnect', lineProtocolStatus:'down', description:'',            bandwidth:0,    interfaceType:''},
        'Management1':{linkStatus:'disabled',   lineProtocolStatus:'down', description:'oob',         bandwidth:1e9,  interfaceType:'1000BASE-T'},
      }}),
    UNREAD:  () => ({key:'interfaces', state:'UNREAD', reason:'show interfaces status | json \u2014 no answer', as_of:now(), age_s:3.0, frames:[], payload:null}),
  },
  environment: {   // never ABSENT: STRICT multi-read; any sub failed -> UNREAD
    PRESENT: () => ({key:'environment', state:'PRESENT', reason:'2 psus, 6 sensors, 4 trays, all healthy', as_of:now(), age_s:6.0,
      frames:[{label:'environment faults', value:0, ceiling:0, status:'OK'}],
      payload:{   /* the RATIFIED deep-cap contract shape — the translator's own output */
        sensors:[
          {name:'Cpu',     status:'ok', fault:false, tempC:38, critC:105, warnC:95},
          {name:'Inlet',   status:'ok', fault:false, tempC:33, critC:70,  warnC:60},
          {name:'Switch',  status:'ok', fault:false, tempC:52, critC:105, warnC:90},
          {name:'Fpga',    status:'ok', fault:false, tempC:49, critC:105, warnC:95},
          {name:'Board',   status:'ok', fault:false, tempC:31, critC:95,  warnC:80},
          {name:'Hotspot', status:'ok', fault:false, tempC:58, critC:75,  warnC:55}],
        fans:[
          {name:'1/1', status:'ok', fault:false, speedPct:35},
          {name:'2/1', status:'ok', fault:false, speedPct:36},
          {name:'3/1', status:'ok', fault:false, speedPct:34},
          {name:'4/1', status:'ok', fault:false, speedPct:35},
          {name:'FanP1/1', status:'ok', fault:false},
          {name:'FanP2/1', status:'ok', fault:false}],
        power:[
          {name:'PSU-1', status:'ok', fault:false, model:'PWR-500AC-F', watts:58.8, capacityW:500, ampsIn:0.36, ampsOut:4.72},
          {name:'PSU-2', status:'ok', fault:false, model:'PWR-500AC-F', watts:63.7, capacityW:500, ampsIn:0.41, ampsOut:5.36}],
        ambientC:28.75, coolingStatus:'coolingOk', tempStatus:'temperatureOk'}}),
    UNREAD:  () => ({key:'environment', state:'UNREAD', reason:'env incomplete \u2014 cooling:no_json_found', as_of:now(), age_s:6.0, frames:[], payload:null}),
  },
  optics: {   // vendor-specific (juniper manifest only): never ABSENT; frame = modules the box alarm-flagged
    PRESENT: () => ({key:'optics', state:'PRESENT', reason:'2 modules (3 lanes), 0 alarmed, 0 warned, hottest 30.5C (et-0/1/0)', as_of:now(), age_s:7.0,
      frames:[{label:'optic alarms', value:0, ceiling:0, status:'OK'}],
      payload:{modules:[
        {name:'et-0/1/0', dom:true, fault:false, warn:false, alarms:[], tempC:30.5, volts:3.224,
         tempCritC:75, tempWarnC:70, tempLowCritC:-5, tempLowWarnC:0,
         rxCritDbm:5.5, rxWarnDbm:4.5, rxLowCritDbm:-14.6, rxLowWarnDbm:-10.6,
         lanes:[
           {lane:0, biasMa:41.675, txDbm:2.20, rxDbm:-4.04, fault:false, warn:false, rxAlarm:false, rxWarn:false, biasAlarm:false, biasWarn:false},
           {lane:1, biasMa:41.414, txDbm:2.48, rxDbm:-3.22, fault:false, warn:false, rxAlarm:false, rxWarn:false, biasAlarm:false, biasWarn:false}]},
        {name:'et-0/1/9', dom:true, fault:false, warn:false, alarms:[], tempC:29.7, volts:3.255,
         tempCritC:78, tempWarnC:75, tempLowCritC:-6, tempLowWarnC:-3,
         rxCritDbm:4.5, rxWarnDbm:3.5, rxLowCritDbm:-17.52, rxLowWarnDbm:-14.51,
         lanes:[
           {lane:0, biasMa:40.585, txDbm:1.22, rxDbm:-0.92, fault:false, warn:false, rxAlarm:false, rxWarn:false, biasAlarm:false, biasWarn:false}]}]}}),
    UNREAD:  () => ({key:'optics', state:'UNREAD', reason:'not read: no physical-interface entries (optic-less shape uncaptured; never ABSENT on empty)', as_of:now(), age_s:7.0, frames:[], payload:null}),
  },
  transceivers: {   // never ABSENT: a chassis inventory can't be positively absent; frameless
    PRESENT: () => ({key:'transceivers', state:'PRESENT', reason:'3/6 slots populated', as_of:now(), age_s:5.0,
      frames:[],                                 /* inventory asserts no fault -> frameless */
      payload:{                                  /* = show inventory | json .xcvrSlots (generic names) */
        '1':{mfgName:'Arista',  modelName:'QSFP-100G-SR4', serialNum:'XCV0000001'},
        '2':{mfgName:'Arista',  modelName:'QSFP-100G-SR4', serialNum:'XCV0000002'},
        '3':{mfgName:'Not Present'},
        '4':{mfgName:'Not Present'},
        '5':{mfgName:'Generic', modelName:'SFP-10G-SR',    serialNum:'XCV0000005'},
        '6':{mfgName:'Not Present'},
      }}),
    UNREAD:  () => ({key:'transceivers', state:'UNREAD', reason:'show inventory | json \u2014 no answer from device', as_of:now(), age_s:5.0, frames:[], payload:null}),
  },
  proc: {   // COMPUTE — never ABSENT (process table always exists); CPU%-vs-100 frame (status = tunable policy)
    PRESENT: () => ({key:'proc', state:'PRESENT', reason:'cpu 12.4% used, 148 processes', as_of:now(), age_s:4.0,
      frames:[{label:'cpu utilization', value:12.4, ceiling:100, status:'OK'}],
      payload:{                                  /* = show processes top once | json */
        cpuInfo:{'%Cpu(s)':{user:8.1, system:3.4, nice:0.0, idle:87.6, ioWait:0.5, hwIrq:0.4}},
        processes:{
          '1604':{cmd:'top',        cpuPct:6.2, residentMem:'5316'},   /* the self-sample — filtered from the list */
          '1893':{cmd:'Bcm',        cpuPct:9.2, residentMem:'142m'},
          '2210':{cmd:'Rib',        cpuPct:3.4, residentMem:'96m'},
          '2044':{cmd:'Sysdb',      cpuPct:2.1, residentMem:'88m'},
          '2455':{cmd:'PhyEthtool', cpuPct:1.9, residentMem:'31m'},
          '2101':{cmd:'Fru',        cpuPct:1.1, residentMem:'40m'},
          '2330':{cmd:'Lldp',       cpuPct:0.8, residentMem:'22m'},
        }}}),
    UNREAD:  () => ({key:'proc', state:'UNREAD', reason:'show processes top once | json \u2014 no answer from device', as_of:now(), age_s:4.0, frames:[], payload:null}),
  },
  version: {   // NOT a panel — the nameplate cap, carried in the feed only so the populate seam can inject mem into COMPUTE
    PRESENT: () => ({key:'version', state:'PRESENT', reason:'', as_of:now(), age_s:8.0, frames:[],
      payload:{modelName:'DCS-7280SR', version:'4.27.3M', serialNumber:'JPE00000000', memTotal:8069000, memFree:4200000}}),
  },
};
// initial states match the mockup trio: present / absent / unread
const cur = { bgp:'PRESENT', ospf:'ABSENT', lldp:'PRESENT', interfaces:'PRESENT', environment:'PRESENT', transceivers:'PRESENT', proc:'PRESENT' };
function mockFeed(){ const f={}; for (const k of ORDER) f[k] = CANS[k][cur[k]]();
  f.version = CANS.version.PRESENT();   // nameplate cap: fed for the COMPUTE mem seam, not rendered as a panel
  return f; }

/* the demo state-toggle UI (not part of hud/) */
function buildDriver(){
  document.getElementById('driver').innerHTML =
    ORDER.map(k => `<div class="grp"><b>${k}</b>` +
      Object.keys(CANS[k]).map(s =>
        `<button data-cap="${k}" data-state="${s}">${s[0]}${s.slice(1,3).toLowerCase()}</button>`).join('') +
      `</div>`).join('');
  document.querySelectorAll('.driver button').forEach(b => b.onclick = () => {
    cur[b.dataset.cap] = b.dataset.state; applyFeed(mockFeed()); markDriver();
  });
  markDriver();
}
function markDriver(){
  document.querySelectorAll('.driver button').forEach(b => {
    const on = cur[b.dataset.cap] === b.dataset.state;
    b.className = on ? 'on ' + b.dataset.state.toLowerCase() : '';
  });
}

buildDriver();
