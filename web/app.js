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

const estado = { q: "", siguiente: null, fotos: [], sondeo: null, cargada: 0 };

function entrar() {
  $("#acceso").classList.add("oculto");
  $("#app").classList.remove("oculto");
  cargar();
}

async function cargar(mas = false) {
  try {
    const params = new URLSearchParams();
    if (estado.q) params.set("q", estado.q);
    if (mas && estado.siguiente) params.set("siguiente", estado.siguiente);
    const r = await api("GET", `/fotos?${params}`);
    estado.fotos = mas ? estado.fotos.concat(r.fotos) : r.fotos;
    if (!mas) estado.cargada = Date.now();
    estado.siguiente = r.siguiente;
    pintar();
    if (!mas) cargarEtiquetas().catch(() => {});
  } catch (e) { if (e.message !== "sesión cerrada") aviso(e.message); }
}

async function cargarEtiquetas() {
  const { etiquetas } = await api("GET", "/etiquetas");
  $("#chips").replaceChildren(...etiquetas.slice(0, 14).map((e) =>
    h("button", { className: "chip" + (normal(e.etiqueta) === normal(estado.q) ? " activo" : ""), onclick: () => buscar(e.etiqueta) },
      h("b", { textContent: e.etiqueta }), ` ${e.fotos}`)));
}

const normal = (t) => t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

function buscar(q) {
  if (!$("#vista-mapa").classList.contains("oculto")) verMapa(false);
  $("#q").value = q;
  estado.q = q.trim();
  cargar();
}

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
  return h("button", { className: "foto", onclick: () => abrir(f.id), title: (f.parecida ? "Parecida a tu búsqueda · " : "") + (f.nombre || "") }, contenido);
}

function pintar() {
  $("#grilla").replaceChildren(...estado.fotos.map(tarjeta));
  $("#vacio").classList.toggle("oculto", estado.fotos.length > 0 || !!estado.q);
  $("#btn-mas").classList.toggle("oculto", !estado.siguiente);
  const eb = $("#estado-busqueda");
  eb.classList.toggle("oculto", !estado.q);
  const parecidas = estado.fotos.filter((f) => f.parecida).length;
  eb.textContent = estado.q ? `${estado.fotos.length} foto(s) para “${estado.q}”` + (parecidas ? ` · ${parecidas} por parecido` : "") : "";

  // Mientras alguna foto se esté revelando, refresca la galería cada 3 s
  clearTimeout(estado.sondeo);
  if (!estado.q && estado.fotos.some((f) => f.estado === "PROCESANDO")) estado.sondeo = setTimeout(() => cargar(), 3000);
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
  $("#d-etiquetas").replaceChildren(...(f.etiquetas || []).map((e) =>
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

  $("#d-borrar").onclick = async () => {
    if (!confirm("¿Borrar esta foto para siempre?")) return;
    try {
      await api("DELETE", `/fotos/${id}`);
      $("#dialogo").close();
      estado.fotos = estado.fotos.filter((x) => x.id !== id);
      pintar();
      cargarEtiquetas();
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
