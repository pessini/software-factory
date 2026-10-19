import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { defineRpcContract, type BbPluginApi } from "@get-bb/plugin-sdk";
import { z } from "zod";

const requestSchema = z.object({
  threadId: z.string().min(1),
  action: z.enum(["read", "change"]),
  config_id: z.string().optional(),
  value: z.string().max(10000).optional(),
}).strict();
const resultSchema = z.object({
  sessionId: z.string(),
  configOptions: z.array(z.object({
    id: z.string(), name: z.string(), description: z.string().nullable(),
    currentValue: z.string(), options: z.array(z.object({ value: z.string(), name: z.string() })),
  })),
});
export const rpcContract = defineRpcContract({ settings: { input: requestSchema, output: resultSchema } });
export type SettingsResult = z.infer<typeof resultSchema>;

export default function plugin(bb: BbPluginApi) {
  const settings = bb.settings.define({ executable: {
    type: "string", label: "Software Factory executable", default: "",
    description: "Leave empty to use the registered Software Factory agent command.",
  }});
  bb.rpc.register(rpcContract, {
    async settings({ threadId, ...request }) {
      const thread = await bb.sdk.threads.get({ threadId });
      if (!thread.environmentId || !thread.projectId) throw new Error("This task needs a project environment.");
      const environment = await bb.sdk.environments.get({ environmentId: thread.environmentId });
      if (!environment.path) throw new Error("The environment has no workspace path.");
      const workspace = environment.path;
      const localHost = (await readFile(join(bb.server.experimental_dataDir, "host-id"), "utf8")).trim();
      if (environment.hostId !== localHost) {
        throw new Error("Factory controls currently require a workspace on the BB server machine.");
      }
      let command = (await settings.get()).executable;
      if (!command) {
        // BB 0.46 stores agents in provider-acp plugin settings; only older config.json files have this entry.
        const config = JSON.parse(await readFile(join(bb.server.experimental_dataDir, "config.json"), "utf8"));
        const legacy = config.customAcpAgents?.find((a: { id?: string }) => a.id === "software-factory");
        if (!legacy) throw new Error("Set the Software Factory executable in plugin settings.");
        const agent = z.object({ command: z.string().min(1), args: z.array(z.string()).optional() }).parse(legacy);
        if (agent.args && JSON.stringify(agent.args) !== JSON.stringify(["acp"])) {
          throw new Error("Set the Software Factory executable in plugin settings for this custom launch command.");
        }
        command = agent.command;
      }
      return await new Promise<SettingsResult>((resolve, reject) => {
        const child = execFile(command, ["settings-panel", "--workspace", workspace], {
          cwd: workspace, encoding: "utf8", timeout: 120000, maxBuffer: 1024 * 1024,
          env: { ...process.env },
        }, (error, stdout, stderr) => {
          if (error) { reject(new Error(stderr.trim() || error.message)); return; }
          try { resolve(resultSchema.parse(JSON.parse(stdout))); } catch (cause) { reject(cause); }
        });
        child.stdin?.end(JSON.stringify({ ...request, interface_reference: threadId }));
      });
    },
  });
}
