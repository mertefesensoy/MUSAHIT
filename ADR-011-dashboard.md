# ADR-011 · Dashboard

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-009 · ADR-010 · ADR-006

---

## ❯ Context

The operator's primary daily interface with MÜŞAHİT is a local web dashboard. The
dashboard renders the morning briefing in PoI-themed visual form, exposes the data
fabric (clusters, arcs, sources, manual overrides) for ad-hoc exploration, and provides
the controls for arc resolution, cluster promotion/demotion, and source management.

The operator chose FastAPI + Jinja2 + HTMX as the stack. The dashboard runs on
`localhost:8001` and is never exposed externally.

## ❯ Decision

A FastAPI application serves both the HTML dashboard (via Jinja2 templates) and a small
JSON API for HTMX interactions. No JavaScript framework, no build step beyond a standalone
Tailwind CLI.

### Stack

- **FastAPI** · `uvicorn` server · runs on `127.0.0.1:8001` · started by Task Scheduler
  at 06:55 (after the pipeline completes) · kept running all day
- **Jinja2** · server-side templating · all HTML rendered from templates
- **HTMX 1.9+** · `dashboard/static/htmx.min.js` · vendored, no CDN
- **Tailwind CSS 3.4** · standalone CLI · `dashboard/build_css.ps1` invokes
  `tailwindcss-windows-x64.exe -i input.css -o static/main.css` · no Node.js dependency
- **Alpine.js 3.13** · optional · `dashboard/static/alpine.min.js` · vendored · used only
  for tiny client state (mobile menu toggle, audio player controls)
- **Iconify icons** · simple SVG inline · no icon library dependency

### Routes

```
GET  /                          → today's briefing (default landing)
GET  /briefing/today            → today's briefing
GET  /briefing/{date}           → briefing for a specific date
GET  /briefing/audio/today      → audio file (streamed)
GET  /briefing/audio/{date}     → audio file for date
GET  /arcs                      → arc index · filterable by state, category
GET  /arcs/{arc_id}             → arc detail view · timeline of linked clusters
GET  /clusters/{cluster_id}     → cluster detail view · article list
GET  /sources                   → source registry view · band tags · last ingest status
GET  /system                    → system log · pipeline_runs · ingest failures
GET  /search                    → full-text and entity search

POST /actions/promote           → manual_overrides INSERT · cluster promote
POST /actions/demote            → manual_overrides INSERT · cluster demote
POST /actions/resolve           → arc state → RESOLVED
POST /actions/reopen            → arc state → OPEN
POST /actions/merge             → arc merge
POST /actions/split             → cluster split from arc
POST /actions/dismiss           → cluster marked dismissed
```

HTMX requests target `/actions/*` and the server returns updated HTML fragments that
replace the affected DOM nodes. No JSON, no client-side state.

### PoI aesthetic specification

The visual language is the most opinionated part of the dashboard. The aesthetic is
explicit and consistent across all views.

#### Color palette

```css
:root {
    --bg:            #0a0a0a;       /* near-black background */
    --bg-elevated:   #141414;       /* card backgrounds */
    --fg:            #e0e0e0;       /* primary text */
    --fg-muted:      #707070;       /* secondary text */
    --fg-faint:      #404040;       /* tertiary text · dividers */
    --amber:         #ffb000;       /* DEFCON 1-2 · system identity */
    --amber-dim:     #c89000;       /* DEFCON 3 */
    --red:           #ff4040;       /* DEFCON 0 (theoretical) · failures */
    --green:         #50d050;       /* RESOLVED arcs · OK status */
    --blue:          #5090ff;       /* WATCH arcs · primary sources */
    --border:        #202020;       /* card borders */
    --scanline:      rgba(255, 176, 0, 0.03);  /* faint amber scanline overlay */
}
```

#### Typography

```css
font-family: 'IBM Plex Mono', 'JetBrains Mono', 'Consolas', monospace;
font-size: 14px;
line-height: 1.5;
```

All text is monospace. The system identity (`MÜŞAHİT` in the header) uses a slightly
larger weight (`font-weight: 600`) and amber color.

#### Layout primitives

- **Header bar** · top of every view · shows `MÜŞAHİT · YYYY-MM-DD · HH:MM` on the left ·
  `DEFCON [N]` indicator on the right · binary digits `0110 1010 1011...` in the
  far-left and far-right corners (decorative · rotates each load)
- **Section dividers** · ASCII rules in amber-dim: `── DEFCON 1-2 · ÖNCELİKLİ ──`
- **Item cards** · `bg-elevated` background · `border` border · 16px padding · 1rem
  vertical spacing between cards
- **Severity badges** · small inline boxes with DEFCON color · text label
  (`AKUT` · `ŞİDDETLİ` · etc.)
- **Source attribution chips** · small monospace pills · format `anadolu·gov_aligned` ·
  amber for primary, white for others, dimmer for social
- **Scan line overlay** · `dashboard/static/scanlines.css` · faint horizontal lines
  across the viewport · `background: repeating-linear-gradient(0deg, transparent,
  transparent 2px, var(--scanline) 3px)` · gives the CRT terminal feel

#### Animations

Minimal. Only:
- A 2-second amber pulse on the header `DEFCON` indicator if today's peak DEFCON is 1 or 0
- Item cards fade in on briefing load (200ms · staggered by 30ms per card)
- HTMX swap targets get a 100ms opacity flash on update

No bouncing icons, no spinners that pulse, no animations for the sake of it.

#### Sound

The dashboard does not play sound on its own (other than the audio player when activated
by the operator). The PoI Machine doesn't beep at you · neither does MÜŞAHİT.

### View specifications

#### Briefing view (`/briefing/today`)

- Audio player at top
- Renders the briefing markdown → HTML via Markdown-it-py
- Each item card has inline action buttons (`PROMOTE` · `DEMOTE` · `DISMISS` · `OPEN ARC`)
- Source attribution chips are clickable · clicking opens a side panel with the cluster's
  article list
- ARC ids in the prose are clickable · navigate to arc detail
- "Mark as read" button at the very bottom (HTMX `POST /actions/dismiss?type=briefing`)

#### Arc detail view (`/arcs/{arc_id}`)

- Arc headline at top · current summary · state badge · peak DEFCON
- Timeline of linked clusters in reverse chronological order
- Each cluster row · DEFCON · category · headline · source chips · date
- Right panel · entity set · category · embedding similarity heatmap (optional ·
  show only if ≥3 clusters)
- Action buttons · `RESOLVE` · `RENAME` · `MERGE WITH...` · `SPLIT`

#### Source registry view (`/sources`)

- Table · source ID · display name · band · tier · last successful fetch · fragility tag
- Failed sources highlighted with red border
- Each row has a `DETAILS` expander showing the last 7 days of ingest history

#### System view (`/system`)

- `pipeline_runs` history · last 30 days · expandable
- Source health summary
- Manual override audit log
- DB size · model status · disk usage
- Backup history

#### Search view (`/search`)

- Full-text search across `articles.title` and `articles.body` · `clusters.headline`
  and `clusters.summary`
- Entity filter · clicking an entity name anywhere in the dashboard pre-fills this filter
- Date range filter
- Category filter
- DEFCON filter

### Authentication

None. The dashboard binds to `127.0.0.1:8001` only · the operating system enforces that
nothing external can reach it. The operator's laptop is the only machine that ever
accesses it.

A `--bind` flag exists for the FastAPI launcher but the default is `127.0.0.1` and the
operator must explicitly override to expose externally · this is intentionally
inconvenient.

### Mobile

The dashboard is desktop-first. Tailwind breakpoints handle a phone-sized viewport
(`sm:`, `md:`, `lg:`) but the assumption is the operator reads on the laptop. If the
operator wants phone access, they expose via Tailscale or similar · not in scope.

### Startup

A scheduled task `MUSAHIT_DASHBOARD` starts the FastAPI server at 06:55:

```powershell
# task action
C:\Python311\python.exe -m uvicorn musahit.api.app:app --host 127.0.0.1 --port 8001
```

The dashboard keeps running 24/7 once started. A separate task restarts it on system
boot.

## ❯ Consequences

**Positive**
- No JavaScript framework · no `node_modules` · no build step beyond Tailwind CLI
- Server-rendered HTML is fast and renders before any JS executes · matches the
  "terminal-feel" aesthetic of PoI
- HTMX for interactivity gives 90% of SPA benefit at 5% of complexity
- Visual identity is opinionated and consistent · operator's daily artifact has a strong
  character

**Negative**
- Tailwind standalone CLI requires periodic rebuilds when classes change · acceptable ·
  the build script is one command
- The aesthetic may date · the operator may want a refresh after a year · the CSS is
  centralized in `dashboard/static/main.css` · easy to revise
- Server-rendered approach makes some highly-interactive features (drag-to-merge arcs)
  harder · acceptable for v0.1 · operator can use simpler MERGE-by-id UI

## ❯ Alternatives considered

- **React + Tailwind + Vite** · more flexible but more complex · rejected per operator
- **Next.js** · operator knows it from Biotama but overkill for local-only · rejected
- **Streamlit** · quick but aesthetic constraints argued against · rejected
- **Pure FastAPI + Jinja with no HTMX** · viable but the actions (promote, resolve)
  benefit from partial-page updates · HTMX is light enough to keep

## ❯ Open questions

- Whether to add a small embedded keyboard shortcut layer (vim-style `j/k/g/G`) for the
  briefing view · would be a small Alpine.js addition · deferred to polish round
- Whether the entity heatmap on arc detail view is worth implementing · revisit when
  arcs accumulate enough history to be informative
