# Stealth Plan

This file is a memo for a future improvement plan.
It is not implemented yet.

## Goal

Make X posting feel quieter and more stable:

- reduce sign-in prompts
- reduce Chrome restarts
- avoid stealing focus from the user's active window
- avoid visible browser flicker as much as possible
- keep media posting support

## What Is Hard

The following combination is very hard to achieve reliably:

- reuse the user's normal daily Chrome profile
- run fully in the background
- never restart Chrome
- never show any browser window

Reasons:

- the normal Chrome profile is often locked by a running browser
- recent Chrome behavior limits some automation/debugging patterns on real profiles
- X can treat a new automation environment as a fresh device/session
- visible-window automation is fragile when focus changes during posting

## Recommended Direction

Use a dedicated automation Chrome profile instead of the user's everyday profile.

Recommended shape:

1. create one dedicated Chrome profile only for X posting
2. sign in manually once
3. reuse that same profile every time
4. automate the compose page with DOM-level control
5. keep the browser minimized, off-screen, or hidden as much as possible
6. avoid address-bar JavaScript tricks
7. avoid native file picker automation when possible

This is the most realistic balance between stealth, stability, and maintainability.

## Best Practical Architecture

### Option A: Dedicated profile + quiet visible mode

This is the recommended option.

- dedicated X automation profile
- first login is manual
- later runs reuse saved session state
- browser starts minimized or moved off-screen
- posting uses DOM interaction for text, media, and submit
- headless mode is avoided

Pros:

- most stable
- fewer login prompts
- much less window flicker
- easier to debug when something changes on X

Cons:

- not fully invisible
- still depends on browser automation

### Option B: Dedicated profile + long-lived browser worker

Keep one automation browser process alive in the background.

- start once
- keep the logged-in tab or context alive
- web app sends post jobs to the background worker
- browser is not restarted for each post

Pros:

- less session churn
- fewer restarts
- better chance of keeping X trust/session continuity

Cons:

- process management becomes more complex
- crash recovery needs design

### Option C: Fully headless mode

Possible in theory, but not the first choice.

- headless browser
- saved session state
- pure DOM automation

Risks:

- X may behave differently in headless mode
- login checkpoints can become harder to recover from
- media posting can be less reliable

Use this only after a dedicated-profile visible mode is stable.

## Stealth Notes

Stealth should not rely on a single trick.

More important than hiding `webdriver` alone:

- use one stable environment repeatedly
- keep the same profile/session
- avoid creating a fresh browser fingerprint every run
- avoid aggressive open/close cycles
- avoid unnatural timing
- avoid unnecessary login flow touches

Helpful ideas:

- dedicated profile
- stable viewport and locale
- realistic waits around media upload
- DOM interaction instead of keyboard focus hacks
- no address-bar `javascript:` flow

## What To Avoid

- directly controlling the user's daily Default profile every time
- copying a fresh profile on every post
- address-bar JavaScript execution
- native file dialog automation if DOM file input is available
- full headless as the primary path

## Suggested Rollout

### Phase 1

- introduce one dedicated automation profile
- add clear UI state: not logged in / ready / posting
- add one-time login flow for that dedicated profile

### Phase 2

- switch posting to a persistent background browser worker
- reuse one live browser session for multiple posts
- minimize or hide the automation window better

### Phase 3

- experiment with stronger stealth settings
- test whether a near-headless or hidden-window mode stays reliable

## Success Criteria

This plan is successful if:

- sign-in is usually needed only once
- repeated posts do not restart Chrome every time
- the browser rarely steals focus
- posting text, images, and video still works reliably
- failures are visible in logs and easy to recover from

