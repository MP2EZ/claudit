## Focus: UX / Design System

First detect whether the project has a design system: a design-system, component-library, or tokens package in dependencies; a theme/tokens directory; a design-system config; or imports from one. If none is detected, report a single Info finding saying so and stop.

If one exists: components and UI in scope should use its primitives (tokens, components, spacing, typography, colors) instead of raw values. Flag hardcoded colors, font sizes, spacing, or shadows that should be tokens; one-off components that duplicate or bypass design-system components; ad-hoc layout where the system has layout primitives; and partial adoption (token for color, hardcoded spacing).
