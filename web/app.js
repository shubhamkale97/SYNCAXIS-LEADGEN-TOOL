const $ = (id) => document.getElementById(id);
let rows = [];
let activeSearchArea = null;
let browserCoordinates = null;
let resolvedLocation = null;
let resolvedQuery = "";
let locationTimer = null;

function toast(message) {
  $("toast").textContent = message; $("toast").classList.remove("hidden");
  setTimeout(() => $("toast").classList.add("hidden"), 5000);
}
async function request(url, options) {
  const response = await fetch(url, options); const text = await response.text();
  let body; try { body = JSON.parse(text); } catch { body = text; }
  if (!response.ok) throw new Error(body?.error || `Request failed (${response.status})`); return body;
}
async function checkHealth() {
  try { await request("/api/health"); $("serviceDot").className = "dot live"; $("serviceText").textContent = "Service ready"; }
  catch { $("serviceDot").className = "dot dead"; $("serviceText").textContent = "Service offline"; }
}

function showResolved(location) {
  resolvedLocation = location;
  $("resolvedLabel").textContent = location.label;
  $("resolvedCoordinates").textContent = `${Number(location.lat).toFixed(6)}, ${Number(location.lon).toFixed(6)} · ${location.provider || "exact coordinates"}`;
  $("locationResolution").classList.remove("hidden");
  $("latitude").value = location.lat; $("longitude").value = location.lon;
  setSearchArea(location.lat, location.lon, Number($("radius").value) || 10);
}
async function resolveTypedLocation(force=false) {
  const query = $("location").value.trim();
  if (!query || query === "Current location" || (!force && query.length < 3)) return null;
  if (resolvedLocation && resolvedQuery === query) return resolvedLocation;
  $("resolvedLabel").textContent = "Resolving location…"; $("resolvedCoordinates").textContent = query; $("locationResolution").classList.remove("hidden");
  try { const location = await request(`/api/geocode?q=${encodeURIComponent(query)}`); if (query !== $("location").value.trim()) { if (force) throw new Error("Location changed. Please search again."); return null; } resolvedQuery = query; showResolved(location); return location; }
  catch (error) { if (query === $("location").value.trim()) { resolvedLocation = null; $("locationResolution").classList.add("hidden"); } if(force) throw error; return null; }
}

const value = (row, ...keys) => keys.map((key) => row[key]).find((item) => item != null && item !== "") || "";
const safeUrl = (url) => /^https?:\/\//i.test(url || "") ? url : url ? `https://${url}` : "";
function escapeHtml(input) { const el = document.createElement("div"); el.textContent = String(input || ""); return el.innerHTML; }
function leadKey(row) { return [value(row,"title","name"), value(row,"address"), value(row,"phone")].join("|").toLowerCase(); }

function updateMetrics() {
  const ratings = rows.map((r) => Number(value(r,"review_rating","rating"))).filter(Number.isFinite);
  $("metricTotal").textContent = rows.length;
  $("metricPhone").textContent = rows.filter((r) => value(r,"phone")).length;
  $("metricWebsite").textContent = rows.filter((r) => value(r,"website")).length;
  $("metricRating").textContent = ratings.length ? (ratings.reduce((a,b) => a+b,0) / ratings.length).toFixed(1) : "—";
  ["excelButton","pdfButton","downloadButton","clearButton"].forEach((id) => $(id).disabled = !rows.length);
}
function render() {
  const query = $("filter").value.toLowerCase().trim();
  const numbered = rows.map((row, i) => ({ row, n: i + 1 }));
  const visible = numbered.filter(({ row }) => Object.values(row).join(" ").toLowerCase().includes(query));
  $("resultRows").innerHTML = visible.map(({ row, n }) => {
    const name=value(row,"title","name"), website=value(row,"website"), phone=value(row,"phone"), email=value(row,"emails","email");
    const rating=value(row,"review_rating","rating"), reviews=value(row,"review_count","reviews");
    return `<tr><td class="lead-no">${n}</td><td><strong>${escapeHtml(name||"Untitled business")}</strong>${website?`<a href="${escapeHtml(safeUrl(website))}" target="_blank" rel="noopener">Visit website ↗</a>`:"<span class=muted>No website</span>"}</td><td>${escapeHtml(value(row,"category")||"—")}</td><td>${phone?`<a href="tel:${escapeHtml(phone)}">${escapeHtml(phone)}</a>`:"—"}${email?`<br><a href="mailto:${escapeHtml(email)}">${escapeHtml(email)}</a>`:""}</td><td class="rating">${rating?`★ ${escapeHtml(rating)}`:"—"}${reviews?`<br><small>${escapeHtml(reviews)} reviews</small>`:""}</td><td>${escapeHtml(value(row,"address")||"—")}</td><td><span class="source-pill">${escapeHtml(row._search||"Search")}</span><small class="source-location">${escapeHtml(row._location||"")}</small></td></tr>`;
  }).join("");
  renderLeadMarkers();
  $("resultCount").textContent=rows.length; $("emptyFilter").classList.toggle("hidden",visible.length>0);
  $("emptyFilter").textContent=rows.length?"No leads match that filter.":"No leads yet. Run your first search above and results will appear here."; updateMetrics();
}
function parseCsv(text) {
  const records=[]; let row=[], field="", quoted=false;
  for(let i=0;i<text.length;i++){const c=text[i]; if(quoted&&c==='"'&&text[i+1]==='"'){field+='"';i++;}else if(c==='"')quoted=!quoted;else if(c===','&&!quoted){row.push(field);field="";}else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&text[i+1]==='\n')i++;row.push(field);if(row.some(Boolean))records.push(row);row=[];field="";}else field+=c;}
  if(field||row.length){row.push(field);records.push(row);} const headers=records.shift()||[];
  return records.map((record)=>Object.fromEntries(headers.map((header,i)=>[header,record[i]||""])));
}
function withinSearchArea(row, area) {
  if (!area) return true;
  const rawLat = value(row, "latitude", "lat"), rawLon = value(row, "longitude", "lon", "lng");
  if (rawLat === "" || rawLon === "") return false;
  const lat = Number(rawLat), lon = Number(rawLon), centerLat = Number(area.lat), centerLon = Number(area.lon);
  if (![lat, lon, centerLat, centerLon].every(Number.isFinite) || Math.abs(lat)>90 || Math.abs(lon)>180) return false;
  const radians = n => n * Math.PI / 180;
  const dLat = radians(lat-centerLat), dLon = radians(lon-centerLon);
  const a = Math.sin(dLat/2)**2 + Math.cos(radians(centerLat))*Math.cos(radians(lat))*Math.sin(dLon/2)**2;
  const distance = 6371 * 2 * Math.atan2(Math.sqrt(Math.min(1,a)),Math.sqrt(Math.max(0,1-a)));
  return distance <= Number(area.radiusKm);
}
function addRows(incoming, search, location, area) {
  if (area && activeSearchArea && area !== activeSearchArea) return 0;
  const existing=new Set(rows.map(leadKey)); let added=0;
  incoming.filter(row=>withinSearchArea(row,area)).forEach((row)=>{const item={...row,_search:search,_location:location}, key=leadKey(item);if(key!=="||"&&!existing.has(key)){rows.push(item);existing.add(key);added++;}});
  if(added) render(); return added;
}

const exportFields=[["Business",r=>value(r,"title","name")],["Category",r=>value(r,"category")],["Phone",r=>value(r,"phone")],["Email",r=>value(r,"emails","email")],["Website",r=>value(r,"website")],["Rating",r=>value(r,"review_rating","rating")],["Reviews",r=>value(r,"review_count","reviews")],["Address",r=>value(r,"address")],["Search",r=>r._search],["Search location",r=>r._location]];
const csvCell=(input)=>`"${String(input||"").replaceAll('"','""')}"`;
function saveBlob(blob,filename){const url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=filename;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function downloadCsv(){if(!rows.length)return toast("Add some results before exporting.");const csv=[exportFields.map(([n])=>csvCell(n)).join(","),...rows.map(r=>exportFields.map(([,get])=>csvCell(get(r))).join(","))].join("\r\n");saveBlob(new Blob(["\ufeff",csv],{type:"text/csv;charset=utf-8"}),"syncaxis-leads.csv");}
function downloadExcel(){if(!rows.length)return toast("Add some results before exporting.");const header=`<Row>${exportFields.map(([n])=>`<Cell ss:StyleID="Header"><Data ss:Type="String">${escapeHtml(n)}</Data></Cell>`).join("")}</Row>`;const body=rows.map(r=>`<Row>${exportFields.map(([,get])=>`<Cell><Data ss:Type="String">${escapeHtml(get(r))}</Data></Cell>`).join("")}</Row>`).join("");const xml=`<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Styles><Style ss:ID="Header"><Font ss:Bold="1"/><Interior ss:Color="#DDEFE5" ss:Pattern="Solid"/></Style></Styles><Worksheet ss:Name="Syncaxis Leads"><Table>${header}${body}</Table></Worksheet></Workbook>`;saveBlob(new Blob([xml],{type:"application/vnd.ms-excel"}),"syncaxis-leads.xls");}

async function collectPartial(jobId, search, location, area) {
  try { const response=await fetch(`/api/jobs/${jobId}/download`); if(!response.ok)return 0; return addRows(parseCsv(await response.text()),search,location,area); }
  catch{return 0;}
}
async function streamJob(jobId, search, location, area) {
  let streamed=0;
  for(let attempt=1;attempt<=120;attempt++){
    const added=await collectPartial(jobId,search,location,area); streamed+=added;
    if(added){$("resultsMessage").textContent=`Live: ${streamed} new leads found in this search. Still collecting…`;$("progressText").textContent=`${streamed} leads added live · continuing search`;}
    const job=await request(`/api/jobs/${jobId}`),status=String(job.Status||job.status||"working").toLowerCase();
    if(status==="ok"){streamed+=await collectPartial(jobId,search,location,area);return streamed;}
    if(status==="failed")throw new Error("The search failed. Try a smaller depth or wait before retrying.");
    if(!added)$("progressText").textContent=`Searching Google Maps · status check ${attempt}`;
    await new Promise(resolve=>setTimeout(resolve,3000));
  }
  throw new Error("The search timed out after 6 minutes.");
}

$("searchForm").addEventListener("submit",async(event)=>{
  event.preventDefault();const button=$("searchButton"),keyword=$("keyword").value.trim();button.disabled=true;$("progress").classList.remove("hidden");
  $("resultsMessage").textContent="New results will appear here one by one while the search runs.";$("progressTitle").textContent="Locating your search area…";$("progressText").textContent="Converting the location into map coordinates.";
  try{const manualLat=$("latitude").value.trim(),manualLon=$("longitude").value.trim(),radiusKm=Number($("radius").value);let location;
    if(resolvedLocation&&resolvedQuery===$("location").value.trim())location=resolvedLocation;else if(browserCoordinates)location=browserCoordinates;else if(!$('location').value.trim()&&!$('coordinates').classList.contains('hidden')&&manualLat&&manualLon)location={lat:manualLat,lon:manualLon,label:`Coordinates ${manualLat}, ${manualLon}`,provider:"manual"};else{try{location=await resolveTypedLocation(true);}catch(error){$("coordinates").classList.remove("hidden");throw new Error(error.message || "Automatic geocoding is unavailable. Enter latitude and longitude.");}}
    const area = {lat:location.lat,lon:location.lon,radiusKm};
    if (activeSearchArea && (Math.abs(Number(activeSearchArea.lat)-Number(area.lat))>0.001 || Math.abs(Number(activeSearchArea.lon)-Number(area.lon))>0.001 || activeSearchArea.radiusKm!==radiusKm)) { rows=[]; render(); }
    activeSearchArea=area;
    const locationName = location.provider === "manual" || location.provider === "browser GPS" ? "" : $("location").value.trim();
    const fieldCompany=($("fieldCompany")||{}).value?$("fieldCompany").value.trim():"";
    if(fieldCompany)runFieldCheck(fieldCompany,keyword,locationName||location.label);
    $("progressTitle").textContent=`Finding ${keyword}…`;$("progressText").textContent="Google Maps + keyless OpenStreetMap search running in parallel";
    request("/api/alternative-search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({keyword,lat:location.lat,lon:location.lon,radius_km:radiusKm})})
      .then((payload)=>{const added=addRows(payload.results||[],`${keyword} · OSM`,location.label,area);if(added){$("resultsMessage").textContent=`${added} leads added by the background OpenStreetMap search. Google Maps is still running…`;toast(`${added} alternative map results added.`);}})
      .catch(()=>{});
    const job=await request("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({keyword,lat:location.lat,lon:location.lon,radius_km:radiusKm,location:locationName,depth:Number($("depth").value),email:$("email").checked})});const jobId=job.id||job.ID;if(!jobId)throw new Error("The scraper did not return a job ID.");
    const added=await streamJob(jobId,keyword,location.label,area);$("resultsMessage").textContent=`Search complete. ${added} new leads within ${radiusKm} km of ${location.label} for “${keyword}”.`;toast(`Search complete—${added} new businesses added.`);
  }catch(error){$("resultsMessage").textContent=`Search could not finish: ${error.message}`;toast(error.message);}finally{button.disabled=false;$("progress").classList.add("hidden");}
});

$("coordinateToggle").addEventListener("click",()=>{const opening=$("coordinates").classList.contains("hidden");$("coordinates").classList.toggle("hidden");$("location").required=!opening&&!browserCoordinates;if(opening)$("latitude").focus();});
$("useLocation").addEventListener("click",()=>{if(!navigator.geolocation)return toast("This browser does not provide location access. Enter coordinates instead.");$("useLocation").disabled=true;$("useLocation").textContent="Locating…";navigator.geolocation.getCurrentPosition(({coords})=>{browserCoordinates={lat:String(coords.latitude),lon:String(coords.longitude),label:"Your current location",provider:"browser GPS"};resolvedQuery="Current location";showResolved(browserCoordinates);$("location").value="Current location";$("location").required=false;$("useLocation").textContent="Location ready";$("useLocation").disabled=false;},()=>{toast("Location permission was unavailable. Enter coordinates instead.");$("coordinates").classList.remove("hidden");$("useLocation").textContent="Use my location";$("useLocation").disabled=false;},{enableHighAccuracy:false,timeout:10000,maximumAge:300000});});
$("location").addEventListener("input",()=>{browserCoordinates=null;resolvedLocation=null;resolvedQuery="";$("latitude").value="";$("longitude").value="";$("location").required=true;$("locationResolution").classList.add("hidden");clearTimeout(locationTimer);locationTimer=setTimeout(()=>resolveTypedLocation(),700);});
$("location").addEventListener("blur",()=>resolveTypedLocation());
[$("latitude"),$("longitude")].forEach((input)=>input.addEventListener("input",()=>{resolvedLocation=null;resolvedQuery="";browserCoordinates=null;const lat=$("latitude").value,lon=$("longitude").value;if(lat&&lon){resolvedQuery=$("location").value.trim();showResolved({lat,lon,label:"Manual coordinates",provider:"manual"});}}));
$("radius").addEventListener("input",()=>{$("radiusValue").textContent=`${$("radius").value} km`;if(resolvedLocation)setSearchArea(resolvedLocation.lat,resolvedLocation.lon,Number($("radius").value));});
$("filter").addEventListener("input",render);$("downloadButton").addEventListener("click",downloadCsv);$("excelButton").addEventListener("click",downloadExcel);$("pdfButton").addEventListener("click",()=>{if(rows.length)window.print();});$("clearButton").addEventListener("click",()=>{if(confirm("Clear all collected leads from this page?")){rows=[];render();$("resultsMessage").textContent="Lead list cleared. Run a search to start again.";}});
let mapView=null,mapProvider=null;

async function initMap(){
  if(mapView)return mapView;
  let cfg={provider:"osm",key:""};
  try{cfg=await request("/api/map-config");}catch(e){}
  const el=$("map"); if(!el)return null;
  if(cfg.provider==="google"&&cfg.key&&!(window.google&&window.google.maps)){
    try{await loadGoogleMaps(cfg.key);}catch(e){cfg={provider:"osm",key:""};}
  }
  if(cfg.provider==="google"&&window.google&&window.google.maps){mapView=createGoogleView(el);mapProvider="google";}
  else if(typeof L!=="undefined"){mapView=createLeafletView(el,cfg);mapProvider=cfg.provider==="here"?"here":"osm";}
  refreshMap();
  return mapView;
}

function loadGoogleMaps(key){
  return new Promise((resolve,reject)=>{
    const s=document.createElement("script");
    s.src=`https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}`;
    s.async=true; s.onload=()=>resolve(); s.onerror=()=>reject(new Error("google maps failed to load"));
    document.head.appendChild(s);
  });
}

function pinIcon(label,kind){return L.divIcon({className:"map-pin-wrap",html:`<span class="map-pin ${kind}">${label}</span>`,iconSize:[26,26],iconAnchor:[13,13]});}

function createLeafletView(el,cfg){
  const m=L.map(el).setView([20.5937,78.9629],5);
  const here=cfg.provider==="here"&&cfg.key;
  const url=here?`https://maps.hereapi.com/v3/base/mc/{z}/{x}/{y}/png?size=512&style=explore.day&apiKey=${encodeURIComponent(cfg.key)}`:"https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  L.tileLayer(url,{maxZoom:here?18:19,attribution:here?"(c) HERE":"(c) OpenStreetMap"}).addTo(m);
  const leadLayer=L.layerGroup().addTo(m),companyLayer=L.layerGroup().addTo(m);
  let areaMarker=null,areaCircle=null;
  setTimeout(()=>m.invalidateSize(),200);
  return { kind:"leaflet", map:m,
    setArea(lat,lon,radiusKm){
      const a=Number(lat),o=Number(lon); if(!Number.isFinite(a)||!Number.isFinite(o))return;
      const ll=[a,o],meters=Math.max(50,(Number(radiusKm)||0)*1000);
      if(areaMarker)areaMarker.setLatLng(ll); else areaMarker=L.marker(ll,{icon:pinIcon("","area")}).addTo(m);
      if(areaCircle)areaCircle.setLatLng(ll).setRadius(meters); else areaCircle=L.circle(ll,{radius:meters,color:"#3ddc84",weight:2,fillColor:"#3ddc84",fillOpacity:0.12}).addTo(m);
      m.fitBounds(areaCircle.getBounds(),{padding:[24,24]});
    },
    setLeads(list){
      leadLayer.clearLayers();
      list.forEach((row,idx)=>{
        const lat=Number(value(row,"latitude","lat")),lon=Number(value(row,"longitude","lon","lng"));
        if(!Number.isFinite(lat)||!Number.isFinite(lon)||Math.abs(lat)>90||Math.abs(lon)>180)return;
        L.marker([lat,lon],{icon:pinIcon(idx+1,"lead"),title:value(row,"title","name")})
          .bindPopup(`<strong>${escapeHtml(value(row,"title","name")||("Lead "+(idx+1)))}</strong><br>${escapeHtml(value(row,"address"))}`)
          .addTo(leadLayer);
      });
    },
    addCompany(company,lat,lon,n){
      companyLayer.clearLayers();
      L.marker([lat,lon],{icon:pinIcon(n,"company"),title:company})
        .bindPopup(`<strong>${escapeHtml(company)}</strong><br>shortlisted (field match)`).addTo(companyLayer);
      m.setView([lat,lon],Math.max(m.getZoom(),12));
    }
  };
}

function createGoogleView(el){
  const m=new google.maps.Map(el,{center:{lat:20.5937,lng:78.9629},zoom:5,mapTypeControl:false,streetViewControl:false,fullscreenControl:false});
  let areaMarker=null,areaCircle=null,leadMarkers=[],companyMarker=null;
  return { kind:"google", map:m,
    setArea(lat,lon,radiusKm){
      const c={lat:Number(lat),lng:Number(lon)}; if(!Number.isFinite(c.lat)||!Number.isFinite(c.lng))return;
      const r=Math.max(50,(Number(radiusKm)||0)*1000);
      if(areaMarker)areaMarker.setPosition(c); else areaMarker=new google.maps.Marker({position:c,map:m,label:"•"});
      if(areaCircle){areaCircle.setCenter(c);areaCircle.setRadius(r);} else areaCircle=new google.maps.Circle({center:c,radius:r,map:m,strokeColor:"#3ddc84",strokeWeight:2,fillColor:"#3ddc84",fillOpacity:0.12});
      m.fitBounds(areaCircle.getBounds());
    },
    setLeads(list){
      leadMarkers.forEach(x=>x.setMap(null)); leadMarkers=[];
      list.forEach((row,idx)=>{
        const lat=Number(value(row,"latitude","lat")),lon=Number(value(row,"longitude","lon","lng"));
        if(!Number.isFinite(lat)||!Number.isFinite(lon)||Math.abs(lat)>90||Math.abs(lon)>180)return;
        leadMarkers.push(new google.maps.Marker({position:{lat:lat,lng:lon},map:m,label:String(idx+1),title:value(row,"title","name")||("Lead "+(idx+1))}));
      });
    },
    addCompany(company,lat,lon,n){
      if(companyMarker)companyMarker.setMap(null);
      companyMarker=new google.maps.Marker({position:{lat:Number(lat),lng:Number(lon)},map:m,label:String(n),title:company,icon:"https://maps.google.com/mapfiles/ms/icons/yellow-dot.png"});
      m.setCenter({lat:Number(lat),lng:Number(lon)}); m.setZoom(Math.max(m.getZoom(),12));
    }
  };
}

function setSearchArea(lat,lon,radiusKm){
  const hint=$("mapHint"); if(hint)hint.textContent=`Radius ${Number(radiusKm)||0} km`;
  if(!mapView){initMap();return;}
  mapView.setArea(lat,lon,radiusKm);
}
function refreshMap(){
  if(!mapView)return;
  mapView.setLeads(rows);
  if(resolvedLocation)mapView.setArea(resolvedLocation.lat,resolvedLocation.lon,Number($("radius").value)||10);
}
function renderLeadMarkers(){ if(mapView)mapView.setLeads(rows); else initMap(); }
async function addCompanyMarker(company,locationText){
  if(!mapView)await initMap();
  if(!mapView)return;
  let lat=null,lon=null;
  const q=(locationText||"").trim();
  if(q){try{const g=await request(`/api/geocode?q=${encodeURIComponent(q)}`);lat=Number(g.lat);lon=Number(g.lon);}catch(e){}}
  if(!Number.isFinite(lat)||!Number.isFinite(lon)){if(resolvedLocation){lat=Number(resolvedLocation.lat);lon=Number(resolvedLocation.lon);}}
  if(!Number.isFinite(lat)||!Number.isFinite(lon))return;
  mapView.addCompany(company,lat,lon,rows.length+1);
}

initMap();
render();checkHealth();

async function runFieldCheck(company,field,city){
  const box=$("fieldResult");
  if(!box)return;
  box.classList.remove("hidden");
  box.innerHTML=`<p class="results-message">Checking ${escapeHtml(company)} for "${escapeHtml(field)}" in ${escapeHtml(city)}…</p>`;
  try{
    const p=await request("/api/company-match",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({company,keyword:field,city})});
    const verdict=p.matches===true?"Yes — in this field":p.matches===false?"No — no evidence of this field":"Unknown — no postings found";
    const cls=p.matches===true?"verdict yes":p.matches===false?"verdict no":"verdict unknown";
    const evidence=(p.evidence||[]).slice(0,6).map((e)=>`<li>${escapeHtml(e.title)}${e.in_title?" <small>(title match)</small>":""} — ${escapeHtml((e.matched_terms||[]).join(", "))}</li>`).join("");
    box.innerHTML=`<div class="genre-head"><strong class="${cls}">${escapeHtml(verdict)}</strong>`
      +`<span class="source-pill">${p.confidence!=null?Math.round(p.confidence*100)+"% confidence":""}</span></div>`
      +`<p class="results-message">${p.matched_postings} of ${p.total_postings} of ${escapeHtml(company)}'s postings match "${escapeHtml(field)}"`
      +`${p.reason?" · "+escapeHtml(p.reason):""}</p>`
      +(evidence?`<ul class="genre-titles">${evidence}</ul>`:"");
    if(p.matches===true)addCompanyMarker(company,(p.evidence&&p.evidence[0]&&p.evidence[0].location)||city);
  }catch(error){
    box.innerHTML=`<p class="results-message">Field check failed: ${escapeHtml(error.message)}</p>`;
  }
}
