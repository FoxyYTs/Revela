// Álbum compartido: sin cuenta. El token va en el fragmento (#), que el navegador no manda en el Referer ni al servidor
const $ = (s) => document.querySelector(s);
const token = location.hash.slice(1);
let fotos = [], actual = 0;

function error(texto) {
  $("#nombre").textContent = "Álbum no disponible";
  $("#mensaje").textContent = texto;
  $("#mensaje").hidden = false;
}

function ver(i) {
  actual = (i + fotos.length) % fotos.length;
  const f = fotos[actual];
  $("#v-img").src = f.original;
  $("#v-img").alt = f.descripcion || "Foto";
  $("#v-texto").textContent = f.descripcion || "";
  if (!$("#visor").open) $("#visor").showModal();
}

async function cargar() {
  if (!/^[A-Za-z0-9_-]{32}$/.test(token)) return error("El enlace está incompleto. Pídele a quien lo compartió que te lo envíe de nuevo.");
  let r;
  try { r = await fetch(`${window.CONFIG.api}/compartidos/${token}`); } catch { return error("No hay conexión. Intenta de nuevo."); }
  if (!r.ok) return error(r.status === 404 ? "Este enlace ya venció o lo dejaron de compartir." : "Algo falló. Intenta de nuevo en un momento.");
  const album = await r.json();
  fotos = album.fotos;
  document.title = `${album.nombre} · Revela`;
  $("#nombre").textContent = album.nombre;
  $("#sub").textContent = `${fotos.length} foto(s)`;
  if (!fotos.length) return error("Este álbum todavía no tiene fotos.");
  $("#grilla").replaceChildren(...fotos.map((f, i) => {
    const b = Object.assign(document.createElement("button"), { className: "foto", onclick: () => ver(i) });
    b.append(Object.assign(document.createElement("img"), { src: f.miniatura, alt: f.descripcion || "Foto", loading: "lazy" }));
    return b;
  }));
  // shortcut: las URLs firmadas duran 1 hora; si la pestaña sigue abierta, se recargan antes de que venzan
  setTimeout(cargar, 50 * 60000);
}

$("#v-ant").onclick = () => ver(actual - 1);
$("#v-sig").onclick = () => ver(actual + 1);
$("#visor").onclick = (e) => { if (e.target === $("#visor")) $("#visor").close(); };
document.addEventListener("keydown", (e) => {
  if (!$("#visor").open) return;
  if (e.key === "ArrowLeft") ver(actual - 1);
  if (e.key === "ArrowRight") ver(actual + 1);
});
window.onhashchange = () => location.reload();  // otro enlace pegado en la misma pestaña
cargar();
