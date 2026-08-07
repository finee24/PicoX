import { AppNav } from "@/components/app-nav";
import { DashboardView } from "@/components/dashboard-view";

/**
 * `shared_url` arriva dal Web Share Target dichiarato in `public/manifest.json`:
 * su Android, condividendo un Reel verso Picox il sistema apre
 * `/dashboard?shared_url=…` e il link si trova già nell'input.
 *
 * Alcune app di condivisione mettono l'URL in `text` invece che in `url`,
 * quindi si guarda anche lì e si estrae il primo link.
 */
function extractSharedUrl(params: Awaited<PageProps<"/dashboard">["searchParams"]>) {
  const direct = params.shared_url;
  if (typeof direct === "string" && direct.trim()) return direct.trim();

  const text = params.shared_text;
  if (typeof text === "string") {
    const match = text.match(/https?:\/\/\S+/);
    if (match) return match[0];
  }
  return undefined;
}

export default async function DashboardPage(props: PageProps<"/dashboard">) {
  const params = await props.searchParams;

  return (
    <>
      <AppNav />
      <DashboardView sharedUrl={extractSharedUrl(params)} />
    </>
  );
}
