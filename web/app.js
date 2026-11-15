const C = window.CONFIG;
const $ = (s) => document.querySelector(s);
const MAX_BYTES = 15 * 1024 * 1024;

// Crea elementos sin innerHTML: los nombres y textos vienen de los usuarios y del OCR
function h(tag, props = {}, ...hijos) {
  const el = Object.assign(document.createElement(tag), props);
  el.append(...hijos.flat().filter((x) => x != null));
  return el;
}

// ---------- Sesión con Cognito (API directa, sin SDK) ----------

const sesion = { acceso: null, vence: 0, correo: null };

async function cognito(accion, datos) {
  const r = await fetch(`https://cognito-idp.${C.region}.amazonaws.com/`, {
    method: "POST",
    headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${accion}` },
    body: JSON.stringify({ ClientId: C.clientId, ...datos }),
  });
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(traducirError(j)), { tipo: (j.__type || "").split("#").pop() });
  return j;
}

function traducirError(j) {
  const tipo = (j.__type || "").split("#").pop();
  return {
    NotAuthorizedException: "Correo o contraseña incorrectos",
    UserNotConfirmedException: "Confirma tu correo con el código que te enviamos",
    UsernameExistsException: "Ya existe una cuenta con ese correo",
    InvalidPasswordException: "La contraseña necesita 8 caracteres, letras minúsculas y números",
    CodeMismatchException: "Código incorrecto",
    ExpiredCodeException: "El código venció; regístrate de nuevo para recibir otro",
    LimitExceededException: "Demasiados intentos, espera un momento",
  }[tipo] || j.message || "Algo salió mal";
}

function guardarTokens(auth) {
  sesion.acceso = auth.AccessToken;
  sesion.vence = Date.now() + (auth.ExpiresIn - 60) * 1000;
  try { if (auth.RefreshToken) localStorage.setItem("revela.refresh", auth.RefreshToken); } catch {}
}

async function token() {
  if (sesion.acceso && Date.now() < sesion.vence) return sesion.acceso;
  let refresh = null;
  try { refresh = localStorage.getItem("revela.refresh"); } catch {}
  if (!refresh) throw new Error("sin sesión");
  const r = await cognito("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", AuthParameters: { REFRESH_TOKEN: refresh } });
  guardarTokens(r.AuthenticationResult);
  return sesion.acceso;
}

async function api(metodo, ruta, cuerpo) {
  let r;
  try {
    r = await fetch(C.api + ruta, {
      method: metodo,
      headers: { Authorization: `Bearer ${await token()}`, ...(cuerpo ? { "Content-Type": "application/json" } : {}) },
      body: cuerpo ? JSON.stringify(cuerpo) : undefined,
    });
  } catch (e) {
    if (e.message === "sin sesión" || e.tipo === "NotAuthorizedException") return salir();
    throw e;
  }
  if (r.status === 401) return salir();
  const datos = r.status === 204 ? null : await r.json();
  if (!r.ok) throw new Error(datos?.error || datos?.message || `Error ${r.status}`);
  return datos;
}

function salir() {
  clearTimeout(estado.sondeo);
  try { localStorage.removeItem("revela.refresh"); } catch {}
  sesion.acceso = null;
  mostrarAcceso("entrar");
  throw new Error("sesión cerrada");
}

function mostrarAcceso(form, msj = "") {
  $("#app").classList.add("oculto");
  $("#acceso").classList.remove("oculto");
  for (const f of ["entrar", "registro", "confirmar"]) $(`#form-${f}`).classList.toggle("oculto", f !== form);
  $("#mensaje").textContent = msj;
}

document.querySelectorAll("[data-ir]").forEach((b) => (b.onclick = () => mostrarAcceso(b.dataset.ir)));

function enviando(form, fn) {
  form.onsubmit = async (e) => {
    e.preventDefault();
    const boton = form.querySelector(".primario");
    boton.disabled = true;
    $("#mensaje").textContent = "";
    try { await fn(Object.fromEntries(new FormData(form))); }
    catch (err) { $("#mensaje").textContent = err.message; }
    finally { boton.disabled = false; }
  };
}

enviando($("#form-entrar"), async ({ correo, clave }) => {
  sesion.correo = correo;
  try {
    const r = await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: correo, PASSWORD: clave } });
    guardarTokens(r.AuthenticationResult);
    $("#form-entrar").reset();
    entrar();
  } catch (e) {
    if (e.tipo === "UserNotConfirmedException") {
      await cognito("ResendConfirmationCode", { Username: correo });
      return mostrarAcceso("confirmar", "Te enviamos un código nuevo al correo");
    }
    throw e;
  }
});

enviando($("#form-registro"), async ({ correo, clave }) => {
  await cognito("SignUp", { Username: correo, Password: clave, UserAttributes: [{ Name: "email", Value: correo }] });
  sesion.correo = correo;
  sesion.clave = clave; // solo en memoria, para entrar sin volver a escribirla tras confirmar
  mostrarAcceso("confirmar", `Te enviamos un código a ${correo}`);
});

enviando($("#form-confirmar"), async ({ codigo }) => {
  await cognito("ConfirmSignUp", { Username: sesion.correo, ConfirmationCode: codigo.trim() });
  $("#form-confirmar").reset();
  if (!sesion.clave) return mostrarAcceso("entrar", "Cuenta confirmada, ya puedes entrar");
  const r = await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", AuthParameters: { USERNAME: sesion.correo, PASSWORD: sesion.clave } });
  sesion.clave = null;
  guardarTokens(r.AuthenticationResult);
  entrar();
});

$("#btn-salir").onclick = async () => {
  try {
    const refresh = localStorage.getItem("revela.refresh");
    if (refresh) await cognito("RevokeToken", { Token: refresh });
  } catch {}
  try { salir(); } catch {}
};

// ---------- Galería ----------

const estado = { q: "", persona: null, album: null, albumes: [], eligiendo: false, uniendo: false, siguiente: null, fotos: [], sondeo: null, cargada: 0, personas: { activo: false, personas: [] } };

function entrar() {
  estado.persona = estado.album = null;  // otra cuenta pudo dejar un filtro puesto
  $("#acceso").classList.add("oculto");
  $("#app").classList.remove("oculto");
  cargar();
}

async function cargar(mas = false) {
  try {
    const params = new URLSearchParams();
    if (estado.q) params.set("q", estado.q);
    if (estado.persona) params.set("persona", estado.persona);
    if (mas && estado.siguiente) params.set("siguiente", estado.siguiente);
    const r = estado.album ? await api("GET", `/albumes/${estado.album.id}`) : await api("GET", `/fotos?${params}`);
    if (estado.album) estado.album = r;
    estado.fotos = mas ? estado.fotos.concat(r.fotos) : r.fotos;
    if (!mas) estado.cargada = Date.now();
    estado.siguiente = r.siguiente;
    pintar();
    if (!mas) { cargarEtiquetas().catch(() => {}); cargarPersonas().catch(() => {}); cargarAlbumes().catch(() => {}); }
  } catch (e) { if (e.message !== "sesión cerrada") aviso(e.message); }
}

async function cargarEtiquetas() {
  const { etiquetas } = await api("GET", "/etiquetas");
  $("#chips").replaceChildren(...etiquetas.slice(0, 14).map((e) =>
    h("button", { className: "chip" + (normal(e.etiqueta) === normal(estado.q) ? " activo" : ""), onclick: () => buscar(e.etiqueta) },
      h("b", { textContent: e.etiqueta }), ` ${e.fotos}`)));
}

const normal = (t) => t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

function buscar(q, persona = null, album = null) {
  if (!$("#vista-mapa").classList.contains("oculto")) verMapa(false);
  $("#q").value = q;
  estado.q = q.trim();
  estado.persona = persona;
  estado.album = album;
  estado.eligiendo = estado.uniendo = false;
  cargar();
}

// ---------- Álbumes ----------

async function cargarAlbumes() {
  estado.albumes = (await api("GET", "/albumes")).albumes;
  $("#a-lista").replaceChildren(...estado.albumes.map((a) =>
    h("button", { className: "album", onclick: () => { $("#dlg-albumes").close(); buscar("", null, a); } },
      a.portada ? h("img", { src: a.portada, alt: "" }) : h("span", { className: "sin-portada" }),
      h("span", {}, h("b", { textContent: a.nombre }), h("small", { textContent: `${a.fotos} foto(s)${a.enlace ? " · compartido" : ""}` })))));
  if (!estado.albumes.length) $("#a-lista").replaceChildren(h("p", { textContent: "Todavía no tienes álbumes." }));
}

$("#btn-albumes").onclick = () => { cargarAlbumes().catch((e) => aviso(e.message)); $("#dlg-albumes").showModal(); };
$("#a-cerrar").onclick = () => $("#dlg-albumes").close();
$("#a-nuevo").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const a = await api("POST", "/albumes", { nombre: e.target.nombre.value });
    e.target.reset();
    $("#dlg-albumes").close();
    buscar("", null, a);
  } catch (err) { aviso(err.message); }
};

async function accionAlbum(fn) {
  try { await fn(); await cargar(); } catch (e) { aviso(e.message); }
}

async function copiar(url) {
  try { await navigator.clipboard.writeText(url); aviso("Enlace copiado"); }
  catch { prompt("Copia el enlace:", url); }
}

function barraAlbum() {
  const a = estado.album, ruta = `/albumes/${a.id}`;
  const chip = (textContent, onclick) => h("button", { className: "chip", textContent, onclick });
  return [
    `${estado.fotos.length} foto(s) en «${a.nombre}»`,
    chip("✏️ Renombrar", () => {
      const nombre = prompt("Nombre del álbum", a.nombre);
      if (nombre !== null) accionAlbum(() => api("PUT", ruta, { nombre }));
    }),
    ...(a.enlace
      ? [chip("🔗 Copiar enlace", () => copiar(a.enlace.url)),
         h("small", { textContent: `vence ${fmtFecha(a.enlace.expira)}` }),
         chip("Dejar de compartir", () => accionAlbum(() => api("DELETE", `${ruta}/enlace`)))]
      : [chip("🔗 Compartir", () => {
          const dias = Number(prompt("¿Cuántos días dura el enlace? (1, 7 o 30)", "7"));
          if (dias) accionAlbum(async () => copiar((await api("POST", `${ruta}/enlace`, { dias })).url));
        })]),
    chip("🗑️ Borrar álbum", async () => {
      if (!confirm(`¿Borrar el álbum «${a.nombre}»? Las fotos se quedan en tu galería.`)) return;
      try { await api("DELETE", ruta); buscar(""); } catch (e) { aviso(e.message); }
    }),
    chip("✕ Ver todas", () => buscar("")),
  ];
}

// ---------- Personas (opcional, con consentimiento) ----------

const nombrePersona = (id) => estado.personas.personas.find((p) => p.id === id)?.nombre || "Sin nombre";

// Recorta la miniatura alrededor de la cara: todo en fracciones del ancho de la imagen, recorte cuadrado con margen
function cara(p) {
  const img = h("img", { src: p.portada.miniatura, alt: "" });
  img.onload = () => {
    const { Left: l, Top: t, Width: w, Height: alto } = p.portada.caja;
    const ar = img.naturalHeight / img.naturalWidth;
    const lado = Math.max(w, alto * ar) * 1.6;
    const cx = l + w / 2, cy = (t + alto / 2) * ar;
    Object.assign(img.style, { width: `${100 / lado}%`, left: `${-(cx - lado / 2) / lado * 100}%`, top: `${-(cy - lado / 2) / lado * 100}%` });
  };
  return h("span", { className: "cara" }, img);
}

async function cargarPersonas() {
  estado.personas = await api("GET", "/personas");
  $("#personas").replaceChildren(...estado.personas.personas.map((p) =>
    h("button", { className: "persona" + (estado.persona === p.id ? " activo" : ""), title: `${p.fotos} foto(s)`,
                  onclick: () => estado.uniendo ? unir(p) : buscar("", estado.persona === p.id ? null : p.id) },
      cara(p), h("span", { textContent: p.nombre || "Sin nombre" }))));
  $("#personas").classList.toggle("oculto", !estado.personas.personas.length);
}

async function nombrar() {
  const nombre = prompt("¿Cómo se llama esta persona?", estado.personas.personas.find((p) => p.id === estado.persona)?.nombre || "");
  if (nombre === null) return;
  try { await api("PUT", `/personas/${estado.persona}`, { nombre }); await cargarPersonas(); pintar(); } catch (e) { aviso(e.message); }
}

async function unir(otra) {
  estado.uniendo = false;
  if (otra.id !== estado.persona &&
      confirm(`¿${otra.nombre || "Esa persona"} y ${nombrePersona(estado.persona)} son la misma? Sus fotos quedan juntas y las nuevas se reconocen como una sola.`)) {
    try { await api("POST", `/personas/${estado.persona}/unir`, { persona: otra.id }); await cargarPersonas(); return cargar(); } catch (e) { aviso(e.message); }
  }
  pintar();
}

async function elegirPortada(foto) {
  estado.eligiendo = false;
  try { await api("PUT", `/personas/${estado.persona}`, { portada: foto }); await cargarPersonas(); } catch (e) { aviso(e.message); }
  pintar();
}

function pintarConsentimiento() {
  const { activo } = estado.personas;
  $("#p-estado").textContent = activo ? "Está activado: las fotos nuevas se agrupan por persona." : "Está desactivado.";
  $("#p-activar").classList.toggle("oculto", activo);
  $("#p-desactivar").classList.toggle("oculto", !activo);
}

$("#btn-personas").onclick = () => { pintarConsentimiento(); $("#dlg-personas").showModal(); };
$("#p-cerrar").onclick = () => $("#dlg-personas").close();
$("#p-activar").onclick = async () => {
  try { estado.personas = await api("PUT", "/personas"); pintarConsentimiento(); } catch (e) { aviso(e.message); }
};
$("#p-desactivar").onclick = async () => {
  if (!confirm("Se borran todas las huellas de caras y los nombres que pusiste. ¿Seguir?")) return;
  try {
    estado.personas = await api("DELETE", "/personas");
    $("#dlg-personas").close();
    buscar("");
  } catch (e) { aviso(e.message); }
};

$("#form-buscar").onsubmit = (e) => { e.preventDefault(); buscar($("#q").value); };
$("#q").oninput = () => { if (!$("#q").value && estado.q) buscar(""); };

function tarjeta(f) {
  const contenido = {
    LISTA: () => [h("img", { src: f.miniatura, alt: f.descripcion || f.etiquetas.join(", ") || f.nombre || "Foto", loading: "lazy" }),
                  h("span", { className: "pie", textContent: f.etiquetas.slice(0, 3).join(" · ") })],
    PROCESANDO: () => [h("span", { className: "aviso revelando", textContent: "Revelando…" })],
    CUARENTENA: () => [h("span", { className: "aviso", textContent: "🚫 Oculta por moderación" })],
    ERROR: () => [h("span", { className: "aviso", textContent: "⚠️ No se pudo analizar" })],
  }[f.estado]();
  return h("button", { className: "foto", onclick: () => estado.eligiendo ? elegirPortada(f.id) : abrir(f.id), title: (f.parecida ? "Parecida a tu búsqueda · " : "") + (f.nombre || "") }, contenido);
}

function pintar() {
  $("#grilla").classList.toggle("eligiendo", estado.eligiendo);
  $("#personas").classList.toggle("eligiendo", estado.uniendo);
  $("#grilla").replaceChildren(...estado.fotos.map(tarjeta));
  $("#vacio").classList.toggle("oculto", estado.fotos.length > 0 || !!estado.q || !!estado.persona || !!estado.album);
  $("#btn-mas").classList.toggle("oculto", !estado.siguiente);
  const eb = $("#estado-busqueda");
  eb.classList.toggle("oculto", !estado.q && !estado.persona && !estado.album);
  const parecidas = estado.fotos.filter((f) => f.parecida).length;
  if (estado.album) {
    eb.replaceChildren(...barraAlbum());
  } else if (estado.persona) {
    const cancelar = h("button", { className: "chip", textContent: "Cancelar", onclick: () => { estado.eligiendo = estado.uniendo = false; pintar(); } });
    eb.replaceChildren(...(estado.eligiendo ? ["Toca la foto que quieres de portada", cancelar]
      : estado.uniendo ? ["Toca arriba, en la fila de caras, a la otra persona", cancelar]
      : [`${estado.fotos.length} foto(s) de ${nombrePersona(estado.persona)}`,
         h("button", { className: "chip", textContent: "✏️ Ponerle nombre", onclick: nombrar }),
         h("button", { className: "chip", textContent: "🖼️ Elegir portada", onclick: () => { estado.eligiendo = true; pintar(); } }),
         h("button", { className: "chip", textContent: "🔗 Unir con…", onclick: () => { estado.uniendo = true; pintar(); } }),
         h("button", { className: "chip", textContent: "✕ Ver todas", onclick: () => buscar("") })]));
  } else {
    eb.textContent = estado.q ? `${estado.fotos.length} foto(s) para “${estado.q}”` + (parecidas ? ` · ${parecidas} por parecido` : "") : "";
  }

  // Mientras alguna foto se esté revelando, refresca la galería cada 3 s
  clearTimeout(estado.sondeo);
  if (!estado.q && !estado.persona && !estado.album && estado.fotos.some((f) => f.estado === "PROCESANDO")) estado.sondeo = setTimeout(() => cargar(), 3000);
}

$("#btn-mas").onclick = () => cargar(true);

// Las URLs de las fotos vencen en 1 hora: si la pestaña quedó abierta, se recarga al volver a ella
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !$("#app").classList.contains("oculto") && Date.now() - estado.cargada > 45 * 60000) cargar();
});

// ---------- Detalle ----------

const fmtFecha = (iso) => iso ? new Date(iso).toLocaleString("es-CO", { dateStyle: "long", timeStyle: "short" }) : "—";

async function abrir(id) {
  let f;
  try { f = await api("GET", `/fotos/${id}`); } catch (e) { return aviso(e.message); }
  $("#d-img").src = f.original || "";
  $("#d-img").alt = f.descripcion || (f.etiquetas || []).join(", ");
  $("#d-descripcion").textContent = f.descripcion || "";
  $("#d-img").classList.toggle("oculto", !f.original);
  $("#d-nombre").textContent = f.nombre || "Foto sin nombre";
  $("#d-etiquetas").replaceChildren(
    ...(f.personas || []).map((id) =>
      h("button", { className: "chip activo", textContent: `👤 ${nombrePersona(id)}`, onclick: () => { $("#dialogo").close(); buscar("", id); } })),
    ...(f.etiquetas || []).map((e) =>
      h("button", { className: "chip", textContent: e, onclick: () => { $("#dialogo").close(); buscar(e); } })));

  const filas = [
    ["Tomada", f.fecha_toma ? fmtFecha(f.fecha_toma) : "Sin fecha en la foto"],
    ["Cámara", f.camara || "—"],
    ["Tamaño", f.ancho ? `${f.ancho} × ${f.alto} px` : "—"],
    ["Lugar", f.gps ? [f.lugar?.nombre || `${f.gps.lat}, ${f.gps.lon}`,
                       f.gps_fuente === "subida" ? h("small", { textContent: " (ubicación al subir)", style: "color:var(--suave)" }) : null,
                       h("br"), h("a", { href: "#", textContent: "Ver en el mapa", onclick: (e) => { e.preventDefault(); $("#dialogo").close(); verMapa(true, f.id); } })]
                    : "Sin ubicación"],
    ["Estado", { LISTA: "Lista", PROCESANDO: "Revelando…", CUARENTENA: `En cuarentena (${(f.moderacion || []).join(", ")})`, ERROR: "Error al analizar" }[f.estado]],
  ];
  $("#d-datos").replaceChildren(...filas.flatMap(([k, v]) => [h("dt", { textContent: k }), h("dd", { style: "margin:0" }, v)]));
  $("#d-texto").replaceChildren(...(f.texto ? [h("dt", { textContent: "Texto en la foto", style: "color:var(--suave);margin-bottom:4px" }), h("pre", { textContent: f.texto })] : []));

  $("#d-caras").classList.toggle("oculto", !estado.personas.activo || f.estado !== "LISTA");
  $("#d-caras").onclick = async () => {
    $("#d-caras").disabled = true;
    $("#d-caras").textContent = "Buscando…";
    try {
      const { personas } = await api("POST", `/fotos/${id}/caras`);
      await cargarPersonas();
      aviso(personas.length ? `Encontré ${personas.length} persona(s)` : "No encontré caras en esta foto");
      $("#dialogo").close();
      abrir(id);
    } catch (e) { aviso(e.message); }
    finally { $("#d-caras").disabled = false; $("#d-caras").textContent = "🔄 Buscar caras"; }
  };
  // Agregar a un álbum (o quitar del que se está viendo); solo fotos listas
  const sel = $("#d-album");
  sel.classList.toggle("oculto", f.estado !== "LISTA");
  sel.replaceChildren(h("option", { value: "", textContent: "📚 Álbum…" }),
    ...(estado.album ? [h("option", { value: "quitar", textContent: `Quitar de «${estado.album.nombre}»` })] : []),
    ...estado.albumes.filter((a) => a.id !== estado.album?.id).map((a) => h("option", { value: a.id, textContent: `Agregar a «${a.nombre}»` })),
    h("option", { value: "nuevo", textContent: "Agregar a un álbum nuevo…" }));
  sel.onchange = async () => {
    const valor = sel.value;
    sel.value = "";
    try {
      if (valor === "quitar") {
        await api("PUT", `/albumes/${estado.album.id}`, { quitar: [id] });
        $("#dialogo").close();
        return cargar();
      }
      let album = valor;
      if (valor === "nuevo") {
        const nombre = prompt("Nombre del álbum nuevo");
        if (!nombre) return;
        album = (await api("POST", "/albumes", { nombre })).id;
      }
      await api("PUT", `/albumes/${album}`, { agregar: [id] });
      aviso("Agregada al álbum");
      cargarAlbumes().catch(() => {});
    } catch (e) { aviso(e.message); }
  };
  $("#d-borrar").onclick = async () => {
    if (!confirm("¿Borrar esta foto para siempre?")) return;
    try {
      await api("DELETE", `/fotos/${id}`);
      $("#dialogo").close();
      estado.fotos = estado.fotos.filter((x) => x.id !== id);
      pintar();
      cargarEtiquetas(); cargarPersonas().catch(() => {});
    } catch (e) { aviso(e.message); }
  };
  $("#dialogo").showModal();
}

$("#d-cerrar").onclick = () => $("#dialogo").close();
$("#dialogo").onclick = (e) => { if (e.target === $("#dialogo")) $("#dialogo").close(); };

// ---------- Ubicación al subir ----------

// Se recuerda la preferencia en este navegador; por defecto activada
const casilla = $("#usar-ubicacion");
try { casilla.checked = localStorage.getItem("revela.ubicacion") !== "no"; } catch { casilla.checked = true; }
casilla.onchange = () => { try { localStorage.setItem("revela.ubicacion", casilla.checked ? "si" : "no"); } catch {} };

function miUbicacion() {
  if (!casilla.checked || !navigator.geolocation) return Promise.resolve(null);
  return new Promise((listo) => navigator.geolocation.getCurrentPosition(
    (p) => listo({ lat: p.coords.latitude, lon: p.coords.longitude }),
    () => listo(null), // sin permiso o sin señal: la foto sube igual
    { timeout: 5000, maximumAge: 600000 })); // una posición de hace 10 min llega al instante y sirve igual
}

// ---------- Mapa (Amazon Location + MapLibre) ----------

const mapaEstado = { mapa: null, marcadores: [], cargando: null };

// Con integrity: si el CDN sirviera otro archivo, el navegador no lo ejecuta (la página maneja tokens de sesión)
function cargarScript(src, integrity) {
  return new Promise((ok, falla) => document.head.append(h("script", { src, integrity, crossOrigin: "anonymous", onload: ok, onerror: falla })));
}

async function prepararMapa() {
  if (mapaEstado.mapa) return mapaEstado.mapa;
  const [{ apiKey, region }] = await Promise.all([
    api("GET", "/mapa"),
    window.maplibregl ? null : cargarScript("https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.js",
      "sha512-x8Bt/82VRs91xVTqHDpQ1t8c4qit3QCRbo9BVqnUsyw5yKqzvL4BAgcz7R34BDCiCwx9ypXPTnxPaNnazQz1ag=="),
  ]);
  const mapa = new maplibregl.Map({
    container: "mapa",
    style: `https://maps.geo.${region}.amazonaws.com/v2/styles/Standard/descriptor?key=${encodeURIComponent(apiKey)}&color-scheme=Dark`,
    center: [-75.5, 6.2], zoom: 5,
  });
  mapa.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  await new Promise((ok) => mapa.once("load", ok));
  return (mapaEstado.mapa = mapa);
}

async function verMapa(mostrar, enfocar) {
  $("#btn-mapa").classList.toggle("activo", mostrar);
  $("#btn-mapa").setAttribute("aria-pressed", String(mostrar));
  $("#vista-galeria").classList.toggle("oculto", mostrar);
  $("#vista-mapa").classList.toggle("oculto", !mostrar);
  if (!mostrar) return;

  const aviso = $("#mapa-aviso");
  try {
    const [mapa, { fotos }] = await Promise.all([prepararMapa(), api("GET", "/lugares")]);
    mapa.resize();
    mapaEstado.marcadores.forEach((m) => m.remove());
    mapaEstado.marcadores = fotos.map((f) => new maplibregl.Marker({
      element: h("button", { className: "marcador" + (f.aproximada ? " aproximada" : ""), onclick: () => abrir(f.id),
                             title: [f.lugar, f.descripcion].filter(Boolean).join(" — "),
                             ariaLabel: `Foto en ${f.lugar || "este lugar"}` },
                 h("img", { src: f.miniatura, alt: "" })),
    }).setLngLat([f.lon, f.lat]).addTo(mapa));

    aviso.classList.toggle("oculto", fotos.length > 0);
    aviso.textContent = "Aún no hay fotos con ubicación. Activa “Mi ubicación” antes de subir, o sube fotos con GPS.";
    const foco = fotos.find((f) => f.id === enfocar);
    if (foco) mapa.flyTo({ center: [foco.lon, foco.lat], zoom: 15 });
    else if (fotos.length) {
      const limites = new maplibregl.LngLatBounds();
      fotos.forEach((f) => limites.extend([f.lon, f.lat]));
      mapa.fitBounds(limites, { padding: 70, maxZoom: 15, duration: 600 });
    }
  } catch (e) {
    if (e.message === "sesión cerrada") return;
    aviso.classList.remove("oculto");
    aviso.textContent = `No se pudo cargar el mapa: ${e.message}`;
  }
}

$("#btn-mapa").onclick = () => verMapa($("#vista-mapa").classList.contains("oculto"));

// ---------- Subidas: URL prefirmada → POST directo a S3 ----------

$("#btn-subir").onclick = () => $("#archivos").click();
$("#archivos").onchange = (e) => { subir([...e.target.files]); e.target.value = ""; };

const zona = $("#zona");
zona.ondragover = (e) => { e.preventDefault(); zona.classList.add("soltar"); };
zona.ondragleave = () => zona.classList.remove("soltar");
zona.ondrop = (e) => { e.preventDefault(); zona.classList.remove("soltar"); subir([...e.dataTransfer.files]); };

function aviso(texto) {
  const panel = $("#subidas");
  panel.classList.remove("oculto");
  panel.append(h("div", {}, h("span", { textContent: texto })));
  setTimeout(() => { panel.lastChild?.remove(); if (!panel.children.length) panel.classList.add("oculto"); }, 5000);
}

async function subir(archivos) {
  const panel = $("#subidas");
  panel.classList.remove("oculto");
  const filas = archivos.map((a) => {
    const estadoFila = h("span", { textContent: "En cola" });
    panel.append(h("div", {}, h("span", { textContent: a.name }), estadoFila));
    return estadoFila;
  });

  if (casilla.checked) filas.forEach((f) => (f.textContent = "Ubicando…"));
  const ubicacion = await miUbicacion();
  filas.forEach((f) => (f.textContent = "En cola"));

  // De a 3 a la vez: no satura la red del celular durante la demo
  let i = 0;
  const trabajador = async () => {
    while (i < archivos.length) {
      const n = i++, a = archivos[n], fila = filas[n];
      try {
        if (!["image/jpeg", "image/png"].includes(a.type)) throw new Error("solo JPG o PNG");
        if (a.size > MAX_BYTES) throw new Error("pesa más de 15 MB");
        fila.textContent = "Subiendo…";
        const { foto, url, campos } = await api("POST", "/subidas", { nombre: a.name, tipo: a.type, ...(ubicacion ? { ubicacion } : {}) });
        const form = new FormData();
        Object.entries(campos).forEach(([k, v]) => form.append(k, v));
        form.append("file", a); // el archivo debe ir al final del formulario
        const r = await fetch(url, { method: "POST", body: form }).catch(() => null);
        if (!r?.ok) {
          api("DELETE", `/fotos/${foto}`).catch(() => {}); // que no quede "Revelando…" una foto que nunca llegó
          throw new Error(r ? `S3 respondió ${r.status}` : "Sin conexión con S3");
        }
        fila.textContent = "✓";
        fila.style.color = "var(--ok)";
      } catch (e) {
        fila.textContent = e.message;
        fila.style.color = "var(--acento)";
      }
    }
  };
  await Promise.all([trabajador(), trabajador(), trabajador()]);
  if (estado.q) $("#q").value = estado.q = "";
  cargar();
  setTimeout(() => { panel.replaceChildren(); panel.classList.add("oculto"); }, 4000);
}

// ---------- Inicio ----------

token().then(entrar).catch(() => mostrarAcceso("entrar"));
