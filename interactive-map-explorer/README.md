# Urban Dossier Frontend

This app is the current React frontend shell for Urban Dossier.

## Current role

- renders the offline NYC map experience
- switches between overview lenses:
  - `general`
  - `amenities`
  - `transit`
  - `safety`
- opens a detail panel after the user clicks a point
- refreshes detail preview when radius or priority order changes
- calls final report generation only after the user clicks `Generate Report`

## Local development

Prerequisites:

- Node.js 24
- the root `server.js` process for tiles and API proxy
- the FastAPI backend on port `8090`

Recommended flow:

1. Start the Python backend
2. Start the root Node server on port `3456`
3. In this folder, run `npm install`
4. Run `npm run dev`

The Vite dev server proxies `/api`, `/tiles`, and `/fonts` to the local Node server.
