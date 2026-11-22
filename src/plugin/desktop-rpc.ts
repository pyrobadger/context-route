export const routingRpc = {
  id: "contextroute",
  methods: {
    snapshot: {
      input: {
        type: "object", required: ["sessionID"], additionalProperties: false,
        properties: { sessionID: { type: "string", maxLength: 160 } },
      },
      output: { type: "object", required: ["state"], additionalProperties: true },
    },
  },
  events: {
    updated: {
      schema: {
        type: "object", required: ["sessionID"], additionalProperties: false,
        properties: { sessionID: { type: "string" } },
      },
    },
  },
} as const;
