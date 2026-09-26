<pre id="results"></pre>
<script>
(function(){
  const out=[]; const ok=(name,cond)=>out.push((cond?"PASS ":"FAIL ")+name);
  const key=(k,opts={})=>document.activeElement.dispatchEvent(new KeyboardEvent("keydown",Object.assign({key:k,bubbles:true,cancelable:true},opts)));
  const docKey=(k,opts={})=>document.dispatchEvent(new KeyboardEvent("keydown",Object.assign({key:k,bubbles:true,cancelable:true},opts)));
  const $=s=>document.querySelector(s);
  const input=$("#cmd-input");
  const type=(t)=>{input.focus();input.value=t;input.dispatchEvent(new Event("input",{bubbles:true}));};
  document.body.focus(); docKey("2");
  ok("key 2 opens the checkpoint sheet", !!$(".sheet") && $(".sheet h1").textContent.includes("comp-c"));
  ok("five choices with keys are visible in the sheet foot", document.querySelectorAll(".sheet-foot .choice").length===5);
  docKey("1");
  ok("key 1 opens the confirmation", !!$(".confirm") && $(".confirm h2").textContent==="Approve and run");
  ok("confirmation states the consequence", $(".confirm p").textContent.includes("Pushes kstrl/factory/comp-c"));
  docKey("Escape"); ok("Esc cancels the confirmation and keeps the sheet", !$(".confirm") && !!$(".sheet"));
  docKey("Escape"); ok("Esc closes the sheet", !$(".sheet"));
  docKey("k",{metaKey:true}); ok("cmd+k focuses the command field", document.activeElement===input);
  type("cost?");
  ok("panel opens with the routed answer row", !$("#cmd-panel").classList.contains("hidden") && $("#cmd-panel").textContent.includes("Answer: spend"));
  ok("panel footer cites the recorded latency and tokens", /\d+ ms · [\d,]+ tokens · recorded/.test($(".cmd-foot").textContent));
  key("Enter"); ok("Enter on an ask opens the HUD with the amount", !!$(".hud") && $(".hud .big").textContent.includes("$19.24"));
  type("merge");
  ok("low confidence shows the pick-one list", $("#cmd-panel").textContent.includes("Not sure what you mean") && document.querySelectorAll("#cmd-panel .pbar").length>=3);
  type("approve and merge comp-c"); key("Enter");
  setTimeout(()=>{
    ok("routed decision opens the checkpoint with its confirmation, nothing fired", !!$(".sheet") && !!$(".confirm") && $(".confirm h2").textContent==="Approve and run");
    docKey("Escape"); docKey("Escape");
    type("aprove the merge"); key("Enter"); // codespell:ignore aprove
    ok("decision with no typed target opens the list", !!$(".sheet") && $(".sheet h1").textContent==="Needs you");
    docKey("Escape");
    type("show me failed components this week by cost as a timeline"); key("Enter");
    const fv=$("#fviews .fview"); ok("make a view produces a floating timeline with three marks", !!fv && fv.querySelectorAll(".v-timeline circle").length===3);
    fv.querySelector(".icon-btn").click(); ok("pin moves the view into the dock", !!$("#dock .fview"));
    $("#dock .fview .icon-btn:nth-of-type(2)").click(); ok("close removes the docked view", !$("#dock .fview"));
    type("git push origin main");
    ok("no-match says so", $("#cmd-panel").textContent.includes("Nothing on this page does that"));
    type("word cloud of finding kinds"); key("Enter");
    ok("catalogue gap becomes a component request", !!$("#fviews .request") && $("#fviews .request h4").textContent.includes("word cloud"));
    type("what is the meaning of life");
    ok("unrecorded text is labelled not recorded", $("#cmd-panel").textContent.includes("Not recorded"));
    docKey("Escape"); input.blur(); document.body.focus(); docKey("t"); ok("t switches the theme", document.documentElement.dataset.theme==="light");
    docKey("4"); docKey("r"); ok("retry confirmation names the command", !!$(".confirm") && $(".confirm .cmdline").textContent.includes("ks retry client-commands"));
    $("#results").textContent=out.join("\n")+"\nDONE "+out.filter(x=>x.startsWith("PASS")).length+"/"+out.length;
  },50);
})();
</script>
