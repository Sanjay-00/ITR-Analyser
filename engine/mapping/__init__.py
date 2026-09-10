"""
engine.mapping  -  decide what each harvested line MEANS.

    template_config  the single file to edit for "what goes where": the
                      spreading sheet's row schema/labels and the wording ->
                      bucket SYNONYMS table
    taxonomy         applies template_config's rules - maps labels to
                      buckets, falls back to Gemini for leftovers, computes
                      the derived rows and ratios
"""
