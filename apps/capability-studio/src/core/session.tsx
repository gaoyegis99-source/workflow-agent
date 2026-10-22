import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { Project } from "../shared/types";

type SessionValue = {
  token: string;
  projects: Project[];
  setSession: (token: string, projects: Project[]) => void;
  setProjects: (projects: Project[]) => void;
  clear: () => void;
};

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const queryClient = useQueryClient();
  const value = useMemo<SessionValue>(
    () => ({
      token,
      projects,
      setSession: (nextToken, nextProjects) => {
        setToken(nextToken);
        setProjects(nextProjects);
      },
      setProjects,
      clear: () => {
        setToken("");
        setProjects([]);
        queryClient.clear();
      },
    }),
    [projects, queryClient, token],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession() {
  const value = useContext(SessionContext);
  if (!value) throw new Error("SessionContext unavailable");
  return value;
}
