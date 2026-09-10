import { useCallback, useEffect, useState } from "react";
import { definePluginApp, useRpc } from "@get-bb/plugin-sdk/app";
import type { rpcContract, SettingsResult } from "./server";
import { Button } from "@/components/ui/button";

function FactoryControls({ threadId }: { threadId: string }) {
  const rpc = useRpc<typeof rpcContract>();
  const [settings, setSettings] = useState<SettingsResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const request = useCallback(async (config_id?: string, value?: string) => {
    setBusy(true); setError(null); setSaved(false);
    try {
      const result = await rpc.call("settings", { threadId, action: config_id ? "change" : "read", ...(config_id !== undefined ? {config_id} : {}), ...(value !== undefined ? {value} : {}) });
      setSettings(result); setSaved(Boolean(config_id));
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }, [rpc, threadId]);
  useEffect(() => { void request(); }, [request]);
  return <div className="space-y-5 text-sm">
    <p>Choose the models for the next factory run. Each change is saved. Send your request in the Software Factory task after you set the choices.</p>
    {error && <p role="alert" className="text-destructive whitespace-pre-wrap">{error}</p>}
    {!settings && busy && <p role="status">Reading available models…</p>}
    {settings?.configOptions.map(option => <div key={option.id} className="space-y-2">
      <label className="block font-medium" htmlFor={`factory-${option.id}`}>{option.name}</label>
      <select id={`factory-${option.id}`} className="w-full rounded-md border border-input bg-background p-2 text-foreground"
        disabled={busy} value={option.currentValue} onChange={e => void request(option.id, e.target.value)}>
        {option.options.map(item => <option key={item.value} value={item.value}>{item.name}</option>)}
      </select>
      {option.description && <p className="text-xs text-muted-foreground">{option.description}</p>}
    </div>)}
    {saved && <p role="status">Choices saved for the next run.</p>}
    <Button variant="outline" disabled={busy} onClick={() => void request()}>Reload choices</Button>
  </div>;
}
export default definePluginApp(app => {
  app.slots.threadPanelAction({ id: "factory", title: "Factory", icon: "Settings2", component: FactoryControls });
});
