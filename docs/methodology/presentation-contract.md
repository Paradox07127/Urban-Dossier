# Presentation classification contract

Urban Dossier treats classification as analytical output, not browser styling.
`GET /api/presentation/classes` publishes a versioned contract containing the
method version, code reference, population, strict class edges, colours, and
accessibility measurements. MapLibre, Vega-Lite specs, and the visible legend
consume those values without recomputing quantiles.

## Population and breaks

The univariate population is the set of land-clipped H3 resolution 8 overview
cells for each score category. Five classes are requested and linear quantiles
are used because the score distributions are clustered. Duplicate quantile
edges are removed: tied observations remain tied, and the effective number of
classes can therefore be lower than five. No epsilon or artificial ordering is
introduced merely to fill a legend.

The building histogram describes the frequency of detailed building scores.
It does not set map colours, because mixing its population with overview-cell
breaks would make the same score change colour across views. `building_score`
has no matching H3 overview category, so its composition chart uses disclosed
fixed 20-point bands as a local fallback.

## Palettes and accessibility gate

The single-score palette is the exact five-step BrBG artifact published as
`schemeBrBG[5]` by d3-scale-chromatic/ColorBrewer; it is copied into the Python
contract so the backend does not depend on a JavaScript runtime. The bivariate
map uses Joshua Stevens' established blue-red 3×3 matrix. Every
horizontal and vertical adjacent pair is transformed through 100% protanopia,
deuteranopia, and tritanopia simulation matrices as well as normal vision, then
measured in CIE Lab with CIE76 delta E. Publication requires the minimum
adjacent delta E in every mode to be at least 8. The API exposes the measured
minimums and pass/fail result so this is executable evidence rather than a
design claim.

## Bivariate map

`GET /api/presentation/bivariate` joins the two requested overview categories
by H3 cell, classifies both axes on the server, and returns land-clipped polygon
geometry with `x_class`, `y_class`, and `bivariate_color`. Only cells with both
scores are emitted. The current Transit × Safety artifact contains 985 common
cells. The client fill expression reads `bivariate_color`; it never derives a
class or combines the two scores itself.

If the presentation endpoint is unavailable, ordinary score views disclose and
use fixed 20-point fallback bands. Tile availability is checked independently,
so a classification-service failure cannot hide otherwise valid building
tiles. The bivariate control is shown only with a contract that passes the
published accessibility gate, and an unavailable GeoJSON response renders no
fabricated cells.
