// damselfish-link host half
// Registers an index.html tap that injects a Damselfish dashboard link
// into the DSH web navbar, served on every page load (no client bundle needed).

export const name = 'damselfish-link'
export const inject = ['webserver']

export function apply(ctx) {
  // The webserver service exposes registerIndexTap(html => html) for
  // transforming the index.html before sending it to the browser.
  const ws = ctx.get('webserver')
  if (ws && typeof ws.registerIndexTap === 'function') {
    ws.registerIndexTap((html) => {
      // Inject a script that adds the link to the nav after DOM loads
      const script = `<script>
(function(){
  function addLink(){
    var nav=document.querySelector('nav');
    if(!nav){setTimeout(addLink,500);return;}
    if(document.getElementById('damselfish-nav-link'))return;
    var a=document.createElement('a');
    a.id='damselfish-nav-link';
    a.href='http://127.0.0.1:3086/';
    a.target='_blank';
    a.rel='noopener noreferrer';
    a.textContent='\uD83D\uDC1F Damselfish';
    a.style.cssText='display:inline-flex;align-items:center;gap:4px;padding:6px 12px;margin-left:8px;border-radius:8px;background:rgba(68,114,196,0.15);color:#4472C4;font-size:13px;font-weight:500;text-decoration:none;cursor:pointer;white-space:nowrap;transition:background 0.15s';
    a.addEventListener('mouseenter',function(){a.style.background='rgba(68,114,196,0.3)'});
    a.addEventListener('mouseleave',function(){a.style.background='rgba(68,114,196,0.15)'});
    nav.appendChild(a);
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',addLink)}else{addLink()}
  var obs=new MutationObserver(function(){if(!document.getElementById('damselfish-nav-link'))addLink()});
  obs.observe(document.body,{childList:true,subtree:true});
})();
</script>`
      // Inject before </body>
      if (html.includes('</body>')) {
        return html.replace('</body>', script + '\n</body>')
      }
      return html + script
    })
  }
}
