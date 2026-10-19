import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const { SUPABASE_URL, SUPABASE_ANON_KEY } = window.MYNEWS_CONFIG;
// supabase-js wants the bare project URL; tolerate a pasted /rest/v1 or trailing slash.
const baseUrl = SUPABASE_URL.trim().replace(/\/+$/, "").replace(/\/rest\/v1$/, "");
const sb = createClient(baseUrl, SUPABASE_ANON_KEY);

// --- tiny helpers ---------------------------------------------------------
const $ = (id) => document.getElementById(id);
const views = ["signin", "interests", "confirm", "notify", "today", "archive", "settings"];
function show(view) {
  views.forEach((v) => $(`view-${v}`).classList.toggle("hidden", v !== view));
  const appViews = ["today", "archive", "settings"];
  $("tabs").classList.toggle("hidden", !appViews.includes(view));
  $("signOutBtn").classList.toggle("hidden", view === "signin");
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === view)
  );
}
let toastTimer;
function toast(msg, isError = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}
function todayLocalDate(timezone) {
  // YYYY-MM-DD in the user's own timezone.
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit",
  });
  return fmt.format(new Date());
}

// --- populate hour / timezone selects ------------------------------------
function fillHourSelect(sel, selected) {
  sel.innerHTML = "";
  for (let h = 0; h < 24; h++) {
    const label = new Date(2020, 0, 1, h).toLocaleTimeString([], {
      hour: "numeric", hour12: true,
    });
    const opt = new Option(label, String(h), false, h === selected);
    sel.add(opt);
  }
}
function fillTzSelect(sel, selected) {
  sel.innerHTML = "";
  const zones = (Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [
    "America/Los_Angeles", "America/Denver", "America/Chicago",
    "America/New_York", "Europe/London", "Europe/Berlin", "Asia/Kolkata",
    "Asia/Tokyo", "Australia/Sydney",
  ]);
  const guess = Intl.DateTimeFormat().resolvedOptions().timeZone;
  zones.forEach((z) => sel.add(new Option(z, z, false, z === (selected || guess))));
}

// --- profile state --------------------------------------------------------
let profile = null;
async function loadProfile() {
  const { data: { user } } = await sb.auth.getUser();
  if (!user) return null;
  // maybeSingle: zero rows returns null (not an error), so a brand-new user
  // without a profile row doesn't hard-fail.
  let { data, error } = await sb
    .from("profiles").select("*").eq("id", user.id).maybeSingle();
  if (error) { toast(`Couldn't load profile: ${error.message}`, true); return null; }
  // Self-heal: if the DB trigger never created a row, create it here. RLS's
  // "own profile" policy allows inserting a row where id = auth.uid().
  if (!data) {
    const upsert = await sb.from("profiles")
      .upsert({ id: user.id, email: user.email, active: true }, { onConflict: "id" })
      .select("*").single();
    if (upsert.error) { toast(`Couldn't create profile: ${upsert.error.message}`, true); return null; }
    data = upsert.data;
  }
  profile = data;
  return data;
}

// --- routing after auth ---------------------------------------------------
async function route() {
  const { data: { session } } = await sb.auth.getSession();
  if (!session) { show("signin"); return; }
  await loadProfile();
  if (!profile || !profile.compiled_profile) {
    show("interests");
  } else {
    openToday();
  }
}

// --- sign in --------------------------------------------------------------
// Code-based OTP: the user types the 6-digit code from the email, so sign-in
// completes *inside* whatever context they're in. This is the only flow that
// works for an installed iOS PWA — email links always open in Safari, whose
// storage is separate from the home-screen app, so a link can never sign the
// installed app in. The link still works too (for desktop).
$("sendLinkBtn").onclick = async () => {
  const email = $("email").value.trim();
  if (!email) return toast("Enter your email", true);
  $("sendLinkBtn").disabled = true;
  const { error } = await sb.auth.signInWithOtp({
    email, options: { emailRedirectTo: window.location.origin },
  });
  $("sendLinkBtn").disabled = false;
  if (error) return toast(error.message, true);
  $("otpStep").classList.remove("hidden");
  $("otpCode").focus();
  toast("We emailed you a 6-digit code ✉️");
};

$("verifyOtpBtn").onclick = async () => {
  const email = $("email").value.trim();
  const token = $("otpCode").value.trim();
  // Supabase's OTP length is configurable (commonly 6-7); don't hardcode it.
  if (!/^\d{4,10}$/.test(token)) return toast("Enter the numeric code from your email", true);
  $("verifyOtpBtn").disabled = true;
  const { error } = await sb.auth.verifyOtp({ email, token, type: "email" });
  $("verifyOtpBtn").disabled = false;
  if (error) return toast(error.message, true);
  // onAuthStateChange fires route() on success.
};

$("signOutBtn").onclick = async () => { await sb.auth.signOut(); route(); };

// --- compile interests ----------------------------------------------------
let pendingCompiled = null;
async function compile(text) {
  const { data: { session } } = await sb.auth.getSession();
  const res = await fetch("/api/compile-profile", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${session.access_token}`,
    },
    body: JSON.stringify({ interests_text: text }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || "compile failed");
  return body.compiled_profile;
}

function renderSections(container, compiled) {
  container.innerHTML = "";
  (compiled.sections || []).forEach((s) => {
    const el = document.createElement("div");
    el.className = "section-preview";
    el.innerHTML =
      `<div class="title">${s.emoji || ""} ${s.title || ""}</div>` +
      `<div class="guidance">${s.guidance || ""}</div>`;
    container.appendChild(el);
  });
}

$("compileBtn").onclick = async () => {
  const text = $("interests").value.trim();
  if (text.length < 10) return toast("Tell us a bit more about your interests", true);
  $("compileBtn").disabled = true;
  $("compileBtn").textContent = "Building…";
  try {
    pendingCompiled = await compile(text);
    renderSections($("sectionsPreview"), pendingCompiled);
    fillHourSelect($("hour"), 8);
    fillTzSelect($("tz"), Intl.DateTimeFormat().resolvedOptions().timeZone);
    show("confirm");
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("compileBtn").disabled = false;
    $("compileBtn").textContent = "Build my brief";
  }
};

$("editInterestsBtn").onclick = () => show("interests");

$("saveProfileBtn").onclick = async () => {
  // compiled_profile was already written by the endpoint; save the prefs.
  const { data: { user } } = await sb.auth.getUser();
  const { error } = await sb.from("profiles").update({
    delivery_hour: parseInt($("hour").value, 10),
    timezone: $("tz").value,
    wants_email: $("wantsEmail").checked,
    active: true,
  }).eq("id", user.id);
  if (error) return toast(error.message, true);
  await loadProfile();
  openNotify();
};

// --- install + push notifications ----------------------------------------
const isStandalone = () =>
  window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window;

let deferredInstallPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstallPrompt = e; // Android/desktop Chrome: we can trigger install
  $("installBtn").classList.remove("hidden");
  $("settingsInstallBtn").classList.remove("hidden");
});

async function installApp() {
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
  } else if (isIOS()) {
    $("iosInstall").classList.remove("hidden");
  } else {
    toast("Use your browser menu → Install app");
  }
}

function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  if (!pushSupported()) return toast("Push isn't supported on this browser", true);
  const key = window.MYNEWS_CONFIG.VAPID_PUBLIC_KEY;
  if (!key || key.startsWith("YOUR-")) return toast("Push isn't configured yet", true);
  // iOS only allows push once the PWA is installed to the home screen.
  if (isIOS() && !isStandalone()) {
    $("iosInstall").classList.remove("hidden");
    return toast("On iPhone, add to Home Screen first, then enable", true);
  }
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return toast("Notifications not enabled", true);
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
    const j = sub.toJSON();
    const up = await sb.from("push_subscriptions").upsert({
      user_id: profile.id, endpoint: j.endpoint,
      p256dh: j.keys.p256dh, auth: j.keys.auth,
    }, { onConflict: "endpoint" });
    if (up.error) return toast(up.error.message, true);
    await sb.from("profiles").update({ wants_push: true }).eq("id", profile.id);
    await loadProfile();
    toast("Notifications enabled 🔔");
    return true;
  } catch (e) {
    toast(`Couldn't enable notifications: ${e.message}`, true);
  }
}

function openNotify() {
  show("notify");
  $("iosInstall").classList.toggle("hidden", !(isIOS() && !isStandalone()));
  $("installBtn").classList.toggle("hidden", !deferredInstallPrompt);
}

$("installBtn").onclick = installApp;
$("skipNotifyBtn").onclick = () => { toast("You're all set ☕"); openToday(); };
$("enablePushBtn").onclick = async () => {
  const ok = await enablePush();
  if (ok) openToday();
};

// --- rendering a brief ----------------------------------------------------
// Today and Archive both land here, so the digest id (needed to key feedback)
// travels with the content and there's only one copy of the render logic.
function longDate(localDate) {
  // Noon avoids the date shifting a day under the viewer's UTC offset.
  return new Date(localDate + "T12:00:00").toLocaleDateString([], {
    weekday: "long", month: "long", day: "numeric",
  });
}

async function renderDigest(digest) {
  show("today");
  $("todayTitle").textContent = longDate(digest.local_date);
  $("todayContent").innerHTML = digest.html || `<p class="muted">Empty.</p>`;
  await mountFeedback(digest);
}

async function openToday() {
  show("today");
  $("todayContent").innerHTML = `<div class="spinner">Loading…</div>`;
  $("feedbackBox").classList.add("hidden");
  const localDate = todayLocalDate(profile.timezone);
  const { data } = await sb.from("digests").select("id, html, local_date")
    .eq("user_id", profile.id).eq("local_date", localDate).maybeSingle();

  if (data && data.html) {
    await renderDigest(data);
    return;
  }
  // Nothing to read means nothing to rate — leave the widget hidden.
  $("todayTitle").textContent = longDate(localDate);
  $("todayContent").innerHTML =
    `<p class="muted">No brief yet for today. Your next one arrives at ` +
    `${new Date(2020, 0, 1, profile.delivery_hour).toLocaleTimeString([], { hour: "numeric", hour12: true })}.</p>`;
}

// --- feedback -------------------------------------------------------------
// One editable verdict per brief. Kept in local state and always written whole,
// so a thumb tap can never blank out notes the user already saved.
let currentDigest = null;
let currentFeedback = { rating: null, notes: "" };

function paintFeedback() {
  $("thumbUpBtn").classList.toggle("active", currentFeedback.rating === 1);
  $("thumbDownBtn").classList.toggle("active", currentFeedback.rating === -1);
  $("feedbackNotes").value = currentFeedback.notes || "";
}

async function mountFeedback(digest) {
  currentDigest = digest;
  currentFeedback = { rating: null, notes: "" };
  if (!digest.id) { $("feedbackBox").classList.add("hidden"); return; }

  const { data } = await sb.from("digest_feedback").select("rating, notes")
    .eq("user_id", profile.id).eq("digest_id", digest.id).maybeSingle();
  if (data) {
    currentFeedback = { rating: data.rating ?? null, notes: data.notes || "" };
  }
  paintFeedback();
  $("feedbackBox").classList.remove("hidden");
}

// Thumb taps write directly (no LLM call); a thumb alone doesn't say what to
// change. Comments go through /api/feedback below.
async function saveFeedback({ quiet = false } = {}) {
  if (!currentDigest || !currentDigest.id) return;
  const { error } = await sb.from("digest_feedback").upsert({
    user_id: profile.id,
    digest_id: currentDigest.id,
    rating: currentFeedback.rating,
    notes: currentFeedback.notes || null,
    updated_at: new Date().toISOString(),
  }, { onConflict: "user_id,digest_id" });
  if (error) return toast(error.message, true);
  if (!quiet) toast("Thanks — this tunes your brief ✓");
}

function setRating(rating) {
  // Tapping the active thumb clears it; rating stays nullable so a note alone
  // is still valid feedback.
  currentFeedback.rating = currentFeedback.rating === rating ? null : rating;
  paintFeedback();
  saveFeedback({ quiet: true });
  toast(currentFeedback.rating === null ? "Rating cleared" : "Thanks ✓");
}

$("thumbUpBtn").onclick = () => setRating(1);
$("thumbDownBtn").onclick = () => setRating(-1);

// The comment goes through /api/feedback, which saves it and has the LLM apply
// it to the profile in the same request — tomorrow's brief reflects it.
$("saveFeedbackBtn").onclick = async () => {
  if (!currentDigest || !currentDigest.id) return;
  currentFeedback.notes = $("feedbackNotes").value.trim();
  const btn = $("saveFeedbackBtn");
  btn.disabled = true;
  btn.textContent = currentFeedback.notes ? "Updating your brief…" : "Saving…";
  try {
    const { data: { session } } = await sb.auth.getSession();
    const res = await fetch("/api/feedback", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify({
        digest_id: currentDigest.id,
        rating: currentFeedback.rating,
        notes: currentFeedback.notes,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return toast(body.error || "Couldn't save feedback", true);
    if (body.updated) {
      await loadProfile();
      toast(`Brief updated ✓ ${body.rationale || ""}`.trim());
    } else if (body.reason && body.reason !== "comment unchanged") {
      toast("Feedback saved — couldn't update your brief this time");
    } else {
      toast("Feedback saved ✓");
    }
  } catch (err) {
    toast(err.message || "Couldn't save feedback", true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Save feedback";
  }
};

// --- archive --------------------------------------------------------------
async function openArchive() {
  show("archive");
  const { data } = await sb.from("digests")
    .select("id, local_date, html")
    .eq("user_id", profile.id).order("local_date", { ascending: false }).limit(60);
  const list = $("archiveList");
  list.innerHTML = "";
  if (!data || !data.length) {
    list.innerHTML = `<p class="muted">No past briefs yet.</p>`;
    return;
  }
  data.forEach((d) => {
    const row = document.createElement("div");
    row.className = "archive-item";
    row.innerHTML = `<span>${new Date(d.local_date + "T12:00:00")
      .toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}</span><span>›</span>`;
    // Past briefs are rateable too — that's how a new user builds up enough
    // feedback to tune their profile without waiting weeks for it.
    row.onclick = () => renderDigest(d);
    list.appendChild(row);
  });
}

// --- settings -------------------------------------------------------------
function openSettings() {
  show("settings");
  $("settingsInterests").value = profile.interests_text || "";
  fillHourSelect($("settingsHour"), profile.delivery_hour);
  fillTzSelect($("settingsTz"), profile.timezone);
  $("settingsWantsEmail").checked = !!profile.wants_email;
  $("settingsWantsPush").checked = !!profile.wants_push;
  $("settingsInstallBtn").classList.toggle("hidden", !deferredInstallPrompt);
  loadTuning();
}

// --- tuning history + undo -----------------------------------------------
// Shows the latest change feedback made and a one-step undo. The DB keeps the
// last 5 versions, so undo can be pressed a few times in a row. If the
// migration hasn't been run yet the query errors and the box stays hidden.
async function loadTuning() {
  const box = $("tuningBox");
  box.classList.add("hidden");
  const { data, error } = await sb.from("profile_versions")
    .select("version, source, rationale, compiled_profile")
    .eq("user_id", profile.id)
    .order("version", { ascending: false });
  if (error || !data || !data.length) return;

  const active = data.find((v) => v.version === profile.active_version) || data[0];
  const previous = data.find((v) => v.version < active.version);

  $("tuningTitle").textContent = "Brief tuning";
  $("tuningSummary").textContent = active.source === "learned"
    ? `Last change from your feedback: ${active.rationale || "tuned from your notes."}`
    : "Not tuned yet. Leave a note on a brief and your brief will adjust to it.";
  $("revertProfileBtn").classList.toggle("hidden", !previous);
  $("revertProfileBtn").onclick = () => undoTo(previous);
  box.classList.remove("hidden");
}

async function undoTo(version) {
  if (!version) return;
  if (!confirm("Undo the last change to your brief?")) return;
  // Undo points the profile at an existing version rather than inserting a
  // new one, so it doesn't manufacture history.
  const upd = await sb.from("profiles").update({
    compiled_profile: version.compiled_profile,
    active_version: version.version,
  }).eq("id", profile.id);
  if (upd.error) return toast(upd.error.message, true);
  await loadProfile();
  await loadTuning();
  toast("Undone ✓");
}

$("settingsEnablePushBtn").onclick = () => enablePush();
$("settingsInstallBtn").onclick = installApp;

$("recompileBtn").onclick = async () => {
  const text = $("settingsInterests").value.trim();
  if (text.length < 10) return toast("Tell us a bit more", true);
  $("recompileBtn").disabled = true;
  $("recompileBtn").textContent = "Rebuilding…";
  try {
    await compile(text);
    await loadProfile();
    toast("Brief rebuilt ✓");
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("recompileBtn").disabled = false;
    $("recompileBtn").textContent = "Rebuild brief";
  }
};

$("saveSettingsBtn").onclick = async () => {
  const { error } = await sb.from("profiles").update({
    delivery_hour: parseInt($("settingsHour").value, 10),
    timezone: $("settingsTz").value,
    wants_email: $("settingsWantsEmail").checked,
    wants_push: $("settingsWantsPush").checked,
  }).eq("id", profile.id);
  if (error) return toast(error.message, true);
  await loadProfile();
  toast("Settings saved ✓");
};

// --- tabs -----------------------------------------------------------------
document.querySelectorAll("nav.tabs button").forEach((b) => {
  b.onclick = () => {
    if (b.dataset.tab === "today") openToday();
    else if (b.dataset.tab === "archive") openArchive();
    else openSettings();
  };
});

// --- boot -----------------------------------------------------------------
sb.auth.onAuthStateChange(() => route());
route();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
