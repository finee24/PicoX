import { redirect } from "next/navigation";

/**
 * La root non ha contenuto proprio: `proxy.ts` ha già deciso se l'utente è
 * autenticato, quindi qui si va sempre alla dashboard (e chi non ha sessione
 * è stato mandato a `/login` prima di arrivare).
 */
export default function Home() {
  redirect("/dashboard");
}
