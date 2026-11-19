import { signOut } from "@/app/login/actions";
import { AppSidebar } from "@/components/app-sidebar";
import { BackendUnavailable } from "@/components/backend-unavailable";
import { Button } from "@/components/ui/button";
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar";
import { requireOwner } from "@/lib/auth/session";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  // The real authorisation check, next to the data (the proxy only does a quick optimistic redirect): anything other
  // than "the backend confirms you are the owner" leaves this page.
  const owner = await requireOwner();
  if ("unavailable" in owner) return <BackendUnavailable />;

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger aria-label="Toggle navigation" />
          <div aria-hidden className="bg-border mr-2 h-4 w-px" />
          <span className="text-muted-foreground text-sm">AI Outreach Intelligence</span>
          <span className="text-muted-foreground ml-auto hidden text-xs sm:inline" data-testid="signed-in-as">
            {owner.email}
          </span>
          <form action={signOut}>
            <Button type="submit" variant="ghost" size="sm">
              Sign out
            </Button>
          </form>
        </header>
        <main className="flex-1 p-4 md:p-6">{children}</main>
      </SidebarInset>
    </SidebarProvider>
  );
}
