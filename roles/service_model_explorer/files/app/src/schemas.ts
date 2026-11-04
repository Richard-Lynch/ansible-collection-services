// Schema for the catalogue snapshot (catalogue.json) the app serves.
//
// WHY A SCHEMA AND NOT `JSON.parse() as Snapshot`. The failure this guards against is an
// unparseable or unexpected read presenting as "nothing is there". A bare cast makes a missing `openrouter`
// key read as `undefined`, `?? []` turns that into an empty array, and the UI then renders
// "0 routable models" — a confident, wrong answer that looks like data.
//
// Parsing through zod makes that impossible: a shape we did not expect is an ERROR with a path,
// which the UI renders as "could not load the catalogue", never as an empty list.

import { z } from "zod";

// Pricing is a per-token USD figure encoded as a STRING by OpenRouter ("0.0000004"), not a
// number. Kept as the string it arrives as and converted at the point of display, so a value we
// cannot parse shows as "unknown" rather than silently becoming 0 — a free model and a model
// whose price we failed to read must not look identical.
export const pricingSchema = z
  .object({
    prompt: z.string().optional(),
    completion: z.string().optional(),
    input_cache_read: z.string().optional(),
  })
  .partial()
  .passthrough();

export const architectureSchema = z
  .object({
    input_modalities: z.array(z.string()).default([]),
    output_modalities: z.array(z.string()).default([]),
    tokenizer: z.string().nullable().optional(),
    modality: z.string().nullable().optional(),
    instruct_type: z.string().nullable().optional(),
  })
  .passthrough();

export const openRouterModelSchema = z
  .object({
    id: z.string(),
    name: z.string().optional(),
    // `created` is a unix SECONDS timestamp. Nullable because a catalogue entry without one is
    // plausible and must not fail the whole parse — the UI shows "unknown" for the date.
    created: z.number().nullable().optional(),
    context_length: z.number().nullable().optional(),
    pricing: pricingSchema.optional(),
    architecture: architectureSchema.optional(),
    supported_parameters: z.array(z.string()).nullable().optional(),
    // Present and non-null on a minority of models. A published expiry is
    // advance warning that an id will start 400-ing, so it is surfaced as a filter.
    expiration_date: z.string().nullable().optional(),
  })
  .passthrough();

export const snapshotSchema = z.object({
  // "When the catalogue data last CHANGED", not when the play last ran — the export carries the
  // previous timestamp forward when the data is identical, so the play stays idempotent.
  generated_at: z.string(),
  gateway_host: z.string(),
  // Counts as recorded BY THE EXPORT, so the UI's own figures can be checked against what the
  // play actually saw. Coerced because Ansible renders them as strings through Jinja.
  counts: z.object({
    enumerated: z.coerce.number(),
    openrouter_catalogue: z.coerce.number(),
    declared_picker: z.coerce.number(),
  }),
  // What GET /v1/models returns: the ids Open WebUI is able to put in its picker.
  enumerated: z.array(z.string()),
  // What the gateway declares explicitly, hence both routable AND pickable.
  declared_picker: z.array(z.string()),
  // OpenRouter's catalogue: routable via the `openrouter/*` wildcard whether or not enumerated.
  openrouter: z.array(openRouterModelSchema),
});

export type Snapshot = z.infer<typeof snapshotSchema>;
export type OpenRouterModel = z.infer<typeof openRouterModelSchema>;
