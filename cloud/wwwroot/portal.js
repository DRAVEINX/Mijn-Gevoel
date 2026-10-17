(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { token: sessionStorage.getItem("moodpress.cloud.token") || "", profile: null, workspaces: [], workspaceId: sessionStorage.getItem("moodpress.cloud.workspace") || "", shareKeyBytes: null };
  const categories = ["gevoel","agenda","routine","welzijn","helpkaart","weekrooster","communicatie"];
  const names = {gevoel:"Gevoel",agenda:"Agenda",routine:"Stappenplan",welzijn:"Welzijn",helpkaart:"Helpkaart",weekrooster:"Weekrooster",communicatie:"Communicatie"};
  function message(text, kind="") { const el=$("message"); el.textContent=text; el.className="status "+kind; }
  function b64(bytes) { let s=""; for (const v of bytes) s += String.fromCharCode(v); return btoa(s); }
  function fromB64(value) { return Uint8Array.from(atob(value), c=>c.charCodeAt(0)); }
  function setSections(auth) { $("auth-section").hidden=!auth; $("app-section").hidden=auth; }
  async function api(path, options={}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization","Bearer "+state.token);
    if (state.workspaceId) headers.set("X-MoodPress-Workspace",state.workspaceId);
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type","application/json");
    const response = await fetch(path,{...options,headers,cache:"no-store",credentials:"same-origin"});
    let data=null;
    const type=response.headers.get("content-type")||"";
    if (type.includes("json")) data=await response.json();
    else data=await response.text();
    if (!response.ok) throw new Error((data && data.error) || (data && data.title) || "De aanvraag is mislukt ("+response.status+").");
    return data;
  }
  function userCredentials() {
    return { email:$("email").value.trim(), password:$("password").value };
  }
  async function auth(mode) {
    try {
      const credentials=userCredentials();
      const body=mode==="register"
        ? {...credentials,displayName:$("display-name").value.trim()}
        : credentials;
      const result=await api("/api/v1/auth/"+mode,{method:"POST",body:JSON.stringify(body)});
      state.token=result.accessToken;
      sessionStorage.setItem("moodpress.cloud.token",state.token);
      $("password").value="";
      await loadAccount();
      message(mode==="register"?"Account aangemaakt. Bewaar je wachtwoord veilig.":"Je bent ingelogd.","success");
    } catch(e) { message(e.message,"error"); }
  }
  async function loadAccount() {
    const data=await api("/api/v1/me");
    state.profile=data.user;
    state.workspaces=data.workspaces||[];
    if (!state.workspaces.length) throw new Error("Er is nog geen werkruimte gekoppeld.");
    if (!state.workspaces.some(w=>w.id===state.workspaceId)) {
      const owner=state.workspaces.find(w=>w.role==="owner");
      state.workspaceId=(owner||state.workspaces[0]).id;
    }
    sessionStorage.setItem("moodpress.cloud.workspace",state.workspaceId);
    renderWorkspaces();
    setSections(false);
    await loadWorkspace();
  }
  function activeWorkspace() { return state.workspaces.find(w=>w.id===state.workspaceId) || state.workspaces[0]; }
  function renderWorkspaces() {
    const select=$("workspace-select"); select.replaceChildren();
    for(const w of state.workspaces) { const o=document.createElement("option"); o.value=w.id; o.textContent=w.name+" — "+(w.role==="owner"?"eigenaar":w.role==="ouder_voogd"?"ouder/voogd":w.role); select.append(o); }
    select.value=state.workspaceId;
    $("welcome").textContent="Welkom, "+state.profile.displayName;
    $("account-label").textContent=state.profile.email;
    $("role-label").textContent="Rol in deze werkruimte: "+(activeWorkspace().role==="owner"?"eigenaar":activeWorkspace().role==="ouder_voogd"?"ouder / voogd":activeWorkspace().role);
    const owner=activeWorkspace().role==="owner";
    $("owner-section").hidden=!owner;
    $("create-share-panel").hidden=!owner;
    $("create-suggestion-panel").hidden=owner;
  }
  async function loadWorkspace() {
    state.shareKeyBytes=null;
    $("shares-list").replaceChildren();
    $("suggestions-list").replaceChildren();
    const owner=activeWorkspace().role==="owner";
    if(owner) await loadMembers();
    await loadShares();
    await loadSuggestions();
    await loadSyncStatus();
  }
  async function changeWorkspace() {
    state.workspaceId=$("workspace-select").value;
    sessionStorage.setItem("moodpress.cloud.workspace",state.workspaceId);
    renderWorkspaces();
    try { await loadWorkspace(); } catch(e) { message(e.message,"error"); }
  }
  async function loadMembers() {
    const holder=$("members-list"); holder.replaceChildren();
    try {
      const members=await api("/api/v1/support/members");
      if(!members.length){holder.textContent="Nog geen gekoppelde personen.";return;}
      for(const m of members) {
        const card=document.createElement("article");card.className="item";
        const title=document.createElement("h4");title.textContent=m.displayName+" ("+m.role+")";
        const detail=document.createElement("p");detail.textContent=m.email+" — toegang: "+m.categories.map(c=>names[c]||c).join(", ");
        const button=document.createElement("button");button.className="danger";button.textContent="Toegang intrekken";
        button.addEventListener("click",async()=>{if(!confirm("Toegang van "+m.email+" intrekken?"))return;try{await api("/api/v1/support/members/"+encodeURIComponent(m.id),{method:"DELETE"});await loadMembers();message("Toegang ingetrokken. Eerder opgeslagen kopieën kunnen niet op afstand worden gewist.","success");}catch(e){message(e.message,"error");}});
        card.append(title,detail,button);holder.append(card);
      }
    } catch(e) { holder.textContent=e.message; }
  }
  async function createInvitation() {
    try {
      const chosen=[...$("owner-section").querySelectorAll('input[type="checkbox"]:checked')].map(x=>x.value);
      const result=await api("/api/v1/support/invitations",{method:"POST",body:JSON.stringify({email:$("invite-email").value.trim(),role:$("invite-role").value,categories:chosen,expiresInHours:168})});
      const holder=$("invitation-result");holder.hidden=false;holder.textContent="Deel deze eenmalige code via een veilig kanaal met "+result.email+":\n"+result.code+"\nGeldig tot "+new Date(result.expiresAt).toLocaleString();
      await navigator.clipboard?.writeText(result.code).catch(()=>{});
      message("Uitnodiging gemaakt. De code is niet per e-mail verzonden.","success");
    } catch(e) { message(e.message,"error"); }
  }
  async function acceptInvitation() {
    try {
      const result=await api("/api/v1/support/invitations/accept",{method:"POST",body:JSON.stringify({code:$("invite-code").value.trim()})});
      $("invite-code").value="";
      state.workspaceId=result.workspaceId;
      sessionStorage.setItem("moodpress.cloud.workspace",state.workspaceId);
      await loadAccount();
      message("Uitnodiging geaccepteerd. Je hebt nu de rol "+result.role+".","success");
    } catch(e) { message(e.message,"error"); }
  }
  async function createShare() {
    try {
      const key=getShareKey();
      const payload={title:$("share-title").value.trim(),text:$("share-body").value,createdAt:new Date().toISOString()};
      if(!payload.title||!payload.text.trim()) throw new Error("Vul een titel en inhoud in.");
      const encrypted=await encrypt(payload,key);
      const result=await api("/api/v1/support/shares",{method:"POST",body:JSON.stringify({category:$("share-category").value,ivBase64:encrypted.iv,ciphertextBase64:encrypted.ciphertext})});
      $("share-title").value="";$("share-body").value="";
      await loadShares();message("De informatie is versleuteld gedeeld.","success");
    } catch(e) { message(e.message,"error"); }
  }
  function getShareKey() {
    const raw=$("share-key").value.trim();
    if(!raw) throw new Error("Vul de deelsleutel in of maak eerst een nieuwe sleutel.");
    let decoded;try{decoded=fromB64(raw);}catch{throw new Error("De deelsleutel is geen geldige Base64.");}
    if(decoded.length!==32) throw new Error("Een deelsleutel moet 32 bytes (256 bits) zijn.");
    state.shareKeyBytes=decoded;return decoded;
  }
  async function encrypt(payload,keyBytes) {
    const key=await crypto.subtle.importKey("raw",keyBytes,{name:"AES-GCM"},false,["encrypt"]);
    const iv=crypto.getRandomValues(new Uint8Array(12));
    const plain=new TextEncoder().encode(JSON.stringify(payload));
    const ciphertext=await crypto.subtle.encrypt({name:"AES-GCM",iv},key,plain);
    plain.fill(0);
    return {iv:b64(iv),ciphertext:b64(new Uint8Array(ciphertext))};
  }
  async function decrypt(item,keyBytes) {
    const key=await crypto.subtle.importKey("raw",keyBytes,{name:"AES-GCM"},false,["decrypt"]);
    const plain=await crypto.subtle.decrypt({name:"AES-GCM",iv:fromB64(item.ivBase64)},key,fromB64(item.ciphertextBase64));
    const data=JSON.parse(new TextDecoder().decode(plain));
    new Uint8Array(plain).fill(0);
    return data;
  }
  async function loadShares() {
    const holder=$("shares-list");holder.replaceChildren();
    try {
      const items=await api("/api/v1/support/shares");
      if(!items.length){holder.textContent="Er zijn geen gedeelde items voor deze rol.";return;}
      for(const item of items) {
        const card=document.createElement("article");card.className="item";
        const title=document.createElement("h4");title.textContent=(names[item.category]||item.category)+" — "+new Date(item.createdAt).toLocaleString();
        card.append(title);
        try {
          const payload=await decrypt(item,getShareKey());
          const t=document.createElement("strong");t.textContent=payload.title||"Gedeelde informatie";
          const p=document.createElement("p");p.textContent=payload.text||"";
          card.append(t,p);
        } catch { const p=document.createElement("p");p.className="hint";p.textContent="Versleuteld item. Vul de juiste deelsleutel in en laad opnieuw.";card.append(p); }
        if(activeWorkspace().role==="owner") {
          const button=document.createElement("button");button.className="danger";button.textContent="Item intrekken";
          button.addEventListener("click",async()=>{if(!confirm("Dit gedeelde item intrekken?"))return;try{await api("/api/v1/support/shares/"+encodeURIComponent(item.id),{method:"DELETE"});await loadShares();message("Item ingetrokken.","success");}catch(e){message(e.message,"error");}});
          card.append(button);
        }
        holder.append(card);
      }
    } catch(e) { holder.textContent=e.message; }
  }
  async function sendSuggestion() {
    try {
      const payload={title:$("suggestion-title").value.trim(),text:$("suggestion-body").value.trim(),createdAt:new Date().toISOString()};
      if(!payload.title||!payload.text) throw new Error("Vul een titel en voorstel in.");
      const encrypted=await encrypt(payload,getShareKey());
      await api("/api/v1/support/suggestions",{method:"POST",body:JSON.stringify({category:$("suggestion-category").value,ivBase64:encrypted.iv,ciphertextBase64:encrypted.ciphertext})});
      $("suggestion-title").value="";$("suggestion-body").value="";
      await loadSuggestions();message("Het voorstel is versleuteld verzonden en wacht op beoordeling.","success");
    } catch(e) { message(e.message,"error"); }
  }
  async function loadSuggestions() {
    const holder=$("suggestions-list");holder.replaceChildren();
    try {
      const items=await api("/api/v1/support/suggestions");
      if(!items.length){holder.textContent="Nog geen voorstellen.";return;}
      for(const item of items) {
        const card=document.createElement("article");card.className="item";
        const title=document.createElement("h4");title.textContent=(names[item.category]||item.category)+" — "+item.status;
        card.append(title);
        try {
          const payload=await decrypt({ivBase64:item.ivBase64,ciphertextBase64:item.ciphertextBase64},getShareKey());
          const h=document.createElement("strong");h.textContent=payload.title||"Voorstel";
          const p=document.createElement("p");p.textContent=payload.text||"";card.append(h,p);
        } catch { const p=document.createElement("p");p.className="hint";p.textContent="Vul de juiste deelsleutel in om de inhoud te lezen.";card.append(p); }
        if(activeWorkspace().role==="owner"&&item.status==="pending") {
          for(const decision of ["accepted","rejected"]) {
            const button=document.createElement("button");button.className=decision==="rejected"?"secondary":"";button.textContent=decision==="accepted"?"Accepteren":"Afwijzen";
            button.addEventListener("click",async()=>{try{await api("/api/v1/support/suggestions/"+encodeURIComponent(item.id)+"/decision",{method:"POST",body:JSON.stringify({decision})});await loadSuggestions();message("Voorstel gemarkeerd als "+decision+".","success");}catch(e){message(e.message,"error");}});
            card.append(button);
          }
        }
        holder.append(card);
      }
    } catch(e) { holder.textContent=e.message; }
  }
  async function loadSyncStatus() {
    const el=$("sync-status");
    if(activeWorkspace().role!=="owner"){el.textContent="Alleen de eigenaar kan cloudsynchronisatie beheren.";return;}
    try {
      const result=await api("/api/v1/sync");
      el.textContent="Cloudsnapshot versie "+result.version+", bijgewerkt "+new Date(result.updatedAt).toLocaleString()+", apparaat: "+result.deviceId+".";
    } catch(e) { el.textContent=e.message.toLowerCase().includes("cloudsnapshot")||e.message.includes("404")||e.message.includes("not found")?"Nog geen cloudsnapshot; koppel de app en voer de eerste synchronisatie uit.":e.message; }
  }
  async function logout() {
    try { if(state.token) await api("/api/v1/auth/logout",{method:"POST"}); } catch {}
    state.token="";state.profile=null;state.workspaces=[];state.workspaceId="";state.shareKeyBytes=null;$("share-key").value="";$("password").value="";
    sessionStorage.removeItem("moodpress.cloud.token");sessionStorage.removeItem("moodpress.cloud.workspace");
    setSections(true);message("Je bent uitgelogd.","success");
  }
  $("login-button").addEventListener("click",()=>auth("login"));
  $("register-button").addEventListener("click",()=>auth("register"));
  $("logout-button").addEventListener("click",logout);
  $("workspace-select").addEventListener("change",changeWorkspace);
  $("create-invite-button").addEventListener("click",createInvitation);
  $("accept-invite-button").addEventListener("click",acceptInvitation);
  $("generate-key-button").addEventListener("click",()=>{const key=crypto.getRandomValues(new Uint8Array(32));$("share-key").value=b64(key);state.shareKeyBytes=key;message("Nieuwe 256-bits deelsleutel gemaakt. Bewaar en deel hem veilig; hij wordt niet op de server opgeslagen.","success");});
  $("copy-key-button").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("share-key").value);message("Deelsleutel gekopieerd. Deel deze apart en veilig.","success");}catch{message("Kopiëren lukte niet. Selecteer de sleutel en kopieer hem handmatig.","error");}});
  $("create-share-button").addEventListener("click",createShare);
  $("send-suggestion-button").addEventListener("click",sendSuggestion);
  $("share-key").addEventListener("change",()=>{state.shareKeyBytes=null;loadShares();loadSuggestions();});
  if(state.token) loadAccount().then(()=>message("Sessie hervat.","success")).catch(()=>{sessionStorage.removeItem("moodpress.cloud.token");state.token="";setSections(true);});
  else setSections(true);
})();
