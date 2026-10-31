/** JSON-Forms validation policy for the Workflow Studio. */

import { createAjv } from "@jsonforms/core";

export const workflowStudioAjv = createAjv({
  useDefaults: false,
  coerceTypes: false,
  removeAdditional: false,
});
