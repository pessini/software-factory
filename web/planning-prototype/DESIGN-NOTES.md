# Plan review: layout study

## Brief and audit

Audience: a person reviewing a feature plan before authorizing implementation.
The user explicitly requested a new section order and content presentation.
This is an overhaul of a local product prototype, with the existing review behavior retained.
The design-taste skill is applied to hierarchy, typography, spacing, color, and review.
Its marketing-page patterns are not appropriate to this planning interface.

The previous page used a system font, green accent `#2e614b`, ink `#252d2b`, pale background
`#f4f5f1`, 12px panels, and 6-7px controls. Its three layouts were Workbench, Focus, and
Document first. Sources, versions, decisions, document previews, and approval often had
similar visual weight. The same documents and navigation appeared in several places.
The inferred starting dials were variance 4, motion 1, density 7.

Keep: the green identity, Software Factory name, existing route and variant parameter,
Plan & handoff / Questions / History navigation, source links, grouped answer submission,
exact revision approval, and visible simulation state. There is no indexed public site,
analytics integration, legal text, or SEO migration to preserve.

## Content changes

| Content | Presentation |
| --- | --- |
| Feature goal and limits | Read first, with included and deferred work together |
| Pending document wording | Explicit review action; exact proposed words available on demand |
| Settled decisions | One-line answer, with question and reason in an expandable row |
| Completion checks | Expandable requirements, clearly separated from real test results |
| Approval | Separate from document acceptance; names the exact versions |
| Sources | Named side panel, instead of a permanent duplicate column |
| Simulation controls and state | Dedicated panel, with a persistent Prototype label |

A uses a summary with a review queue. B uses independent review sections and a focused
reader. C uses files and wording as its main navigation. These are different structures,
not color variations. On phones, A moves the next required action above the summary;
B and C turn their side navigation into compact groups above the content.

## Visual decisions

- `DESIGN_VARIANCE: 6`: unequal columns and three distinct content arrangements.
- `MOTION_INTENSITY: 3`: a short panel entrance and direct control feedback. Reduced-motion
  preferences disable these effects.
- `VISUAL_DENSITY: 4`: short summaries first, full evidence on demand.
- Native HTML and custom CSS, not a claimed implementation of a named design system.
- Self-hosted Geist; original Phosphor regular glyphs in a local sprite.
- One restrained green accent; neutral surfaces; one theme across the complete page.
- Controls use 8px corners, main grouped surfaces use 14px, small symbols use smaller
  corners. Lines and spacing separate most content; cards identify pending review or approval.

## Browser verification

Checked in the actual local browser on 2026-09-25:

- All three layouts at 1440px and 390px, plus compact layout checks at 320px and 900px.
- Light and dark themes, layout switching, open/close panels, Escape focus return,
  native radio controls, document acceptance, and explicit plan approval.
- Revisiting a settled second-round answer exposes the correct question.
- A repeated request and a recovered lost response return the same sample run ID.
- Edits after handoff require another review and leave the approved sample copy unchanged.
- No browser warnings or errors during the checked flows.
- JavaScript syntax, HTML resource paths, and no visible em-dash copy.

Light-theme text contrast against the main surfaces is at least 4.55:1 for muted text
and 6.31:1 for accent text. The focus ring is at least 3.80:1. Form boundaries use the
muted token. Dark-theme contrast and primary button colors were also checked.

The applicable preflight checks cover color/theme consistency, shape rules, typography,
copy, desktop navigation, control wrapping, keyboard focus, responsive collapse, and
empty/disconnected/recovery states. The simulation label and revision labels are
functional information in this product interface.

Marketing hero, stock imagery, logo walls, pricing tables, quotes, marquees, animated
stories, React client boundaries, and search ranking checks do not apply. No decorative
image was added to a text-review tool. No production performance score or accessibility
certification is claimed. No automated tests were added under the prototype skill.

## Recommendation

A is the best starting point for the user's stated goal: understand and review the plan
before implementation. B supports a deliberate section review. C supports exact wording
review. The user has not yet selected a layout, so this remains a comparison, not a
validated production decision.
