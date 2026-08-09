/*
 * Refresh the events list from the Google Calendar API.
 *
 * This is the source of the events list. The site is static and only
 * rebuilds when someone pushes, so anything baked in at build time would be
 * stale within weeks -- fetching on load keeps it correct indefinitely.
 *
 * If this cannot run -- JS disabled, network failure, revoked key -- the
 * Google Calendar embed takes over. It does not match the site's styling,
 * but it is always live, which matters more.
 *
 * The API key is public by design (see config.toml) and restricted by HTTP
 * referrer to this site, read-only, against an already-public calendar.
 */
(function () {
  "use strict";

  var LUMA = /https?:\/\/(?:lu\.ma|(?:www\.)?luma\.com)\/[^\s<>"']+/;

  function lumaLink(description) {
    // Only Luma links. Descriptions also carry Zoom and Google Meet joining
    // links, and republishing those invites uninvited guests.
    var m = LUMA.exec(description || "");
    return m ? m[0].replace(/[.,;]+$/, "") : "";
  }

  function el(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    // textContent, never innerHTML: titles and locations are arbitrary text
    // typed into a calendar and must not be able to inject markup.
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function fmt(date, tz, opts) {
    opts.timeZone = tz;
    var out = new Intl.DateTimeFormat("en-GB", opts).format(date);
    // ICU abbreviates September as "Sept". Three letters everywhere reads
    // better in the narrow date badge and matches the other months.
    return out.replace(/\bSept\b/g, "Sep");
  }

  function timeLabel(date, tz) {
    // "6:00pm" rather than "6:00 pm".
    return fmt(date, tz, { hour: "numeric", minute: "2-digit", hour12: true })
      .replace(/\s+/g, "")
      .toLowerCase();
  }

  function card(ev, tz) {
    var isAllDay = !ev.start.dateTime;
    var start = new Date(ev.start.dateTime || ev.start.date + "T00:00:00");

    var li = el("li", "event-item");

    var dateBox = el("div", "event-date");
    dateBox.setAttribute("aria-hidden", "true");
    dateBox.appendChild(el("span", "event-date-day", fmt(start, tz, { day: "numeric" })));
    dateBox.appendChild(el("span", "event-date-month", fmt(start, tz, { month: "short" })));
    li.appendChild(dateBox);

    var body = el("div", "event-body");
    var title = el("h3", "event-title");
    var url = lumaLink(ev.description);
    if (url) {
      var a = el("a", null, ev.summary || "Untitled event");
      a.href = url;
      a.rel = "noopener";
      var icon = el("i", "bi-box-arrow-up-right event-title-ext");
      icon.setAttribute("aria-hidden", "true");
      a.appendChild(icon);
      title.appendChild(a);
    } else {
      title.textContent = ev.summary || "Untitled event";
    }
    body.appendChild(title);

    var meta = el("p", "event-meta mb-0");
    var when = el("span", "text-nowrap");
    var clock = el("i", "bi-clock me-1");
    clock.setAttribute("aria-hidden", "true");
    when.appendChild(clock);
    when.appendChild(
      document.createTextNode(
        fmt(start, tz, { weekday: "short", day: "numeric", month: "short" }) +
          (isAllDay ? " · all day" : ", " + timeLabel(start, tz))
      )
    );
    meta.appendChild(when);

    if (ev.location) {
      var loc = el("span", "event-location");
      var pin = el("i", "bi-geo-alt me-1");
      pin.setAttribute("aria-hidden", "true");
      loc.appendChild(pin);
      loc.appendChild(document.createTextNode(ev.location));
      meta.appendChild(loc);
    }

    body.appendChild(meta);
    li.appendChild(body);
    return li;
  }

  function render(mount, events, tz, compact) {
    if (!events.length) return;
    var ul = el("ul", "event-list list-unstyled mb-0" + (compact ? " event-list--compact" : ""));
    events.forEach(function (ev) {
      ul.appendChild(card(ev, tz));
    });
    mount.innerHTML = "";
    mount.appendChild(ul);
  }

  function refresh(mount) {
    var key = mount.getAttribute("data-api-key");
    var id = mount.getAttribute("data-calendar-id");
    if (!key || !id) return;

    var tz = mount.getAttribute("data-tz") || "America/Chicago";
    var limit = parseInt(mount.getAttribute("data-limit"), 10) || 5;
    var compact = mount.getAttribute("data-compact") === "true";

    var url =
      "https://www.googleapis.com/calendar/v3/calendars/" +
      encodeURIComponent(id) +
      "/events?key=" + encodeURIComponent(key) +
      "&singleEvents=true&orderBy=startTime" +
      "&timeMin=" + encodeURIComponent(new Date().toISOString()) +
      "&maxResults=" + limit;

    fetch(url, { credentials: "omit" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        var items = (data.items || []).filter(function (ev) {
          return ev.status !== "cancelled" && ev.start;
        });
        if (items.length) {
          render(mount, items.slice(0, limit), tz, compact);
        } else {
          // Nothing coming up. The embed at least lets people browse the
          // calendar rather than staring at a blank panel.
          showEmbed(mount, id, tz);
        }
      })
      .catch(function () {
        showEmbed(mount, id, tz);
      });
  }

  /*
   * Last resort: the Google Calendar embed, which is always live. Not styled
   * like the rest of the page, but an empty box would be worse.
   */
  function showEmbed(mount, id, tz) {
    var frame = document.createElement("iframe");
    frame.src =
      "https://calendar.google.com/calendar/embed?src=" + encodeURIComponent(id) +
      "&ctz=" + encodeURIComponent(tz) +
      "&mode=AGENDA&showTitle=0&showPrint=0&showTabs=0&showCalendars=0" +
      "&bgcolor=%23ffffff&color=%230e879e";
    frame.setAttribute("title", "Events calendar");
    frame.setAttribute("frameborder", "0");
    frame.setAttribute("scrolling", "no");
    var wrap = el("div", "calendar-embed calendar-embed--compact shadow-sm");
    wrap.appendChild(frame);
    mount.innerHTML = "";
    mount.appendChild(wrap);
  }

  function init() {
    var mounts = document.querySelectorAll("[data-events-mount]");
    for (var i = 0; i < mounts.length; i++) refresh(mounts[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
