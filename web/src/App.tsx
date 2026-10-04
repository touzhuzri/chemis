import { useEffect, useState } from "react";
import Home from "./pages/Home";
import Workspace from "./pages/Workspace";
import KnowledgePage from "./pages/KnowledgePage";
import ChatPage from "./pages/ChatPage";
import MoleculeExplorer from "./pages/MoleculeExplorer";
import MoleculeLibrary from "./pages/MoleculeLibrary";
import ReactionExplorer from "./pages/ReactionExplorer";
import ReactionDetail from "./pages/ReactionDetail";
import LiteratureSearch from "./pages/LiteratureSearch";
import ResearchSessions from "./pages/ResearchSessions";

/** Hash routing: #/ home, #/research/<job|session>, #/knowledge/<kind>/<id>. */
function useHashRoute(): { path: string[]; go: (hash: string) => void } {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const path = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { path, go: (h: string) => (window.location.hash = h) };
}

export default function App() {
  const { path, go } = useHashRoute();
  return (
    <>
      <header className="topbar">
        <h1>
          <a onClick={() => go("#/")}>CREA</a>
        </h1>
        <span className="tagline">Chemical Research Workbench — 证据与知识，而不是聊天</span>
      </header>
      <main className="layout">
        {path[0] === "research" && path[1] ? (
          <Workspace id={decodeURIComponent(path[1])} go={go} />
        ) : path[0] === "chat" ? (
          <ChatPage id={decodeURIComponent(path[1] || "")} go={go} />
        ) : path[0] === "molecule" ? (
          <MoleculeExplorer smiles={decodeURIComponent(path[1] || "")} go={go} />
        ) : path[0] === "library" ? (
          <MoleculeLibrary go={go} />
        ) : path[0] === "reactions" && path[1] ? (
          <ReactionDetail
            templateId={decodeURIComponent(path[1])}
            from={decodeURIComponent(path[2] || "")}
            go={go}
          />
        ) : path[0] === "reactions" ? (
          <ReactionExplorer go={go} />
        ) : path[0] === "literature" ? (
          <LiteratureSearch paperId={decodeURIComponent(path[1] || "")} go={go} />
        ) : path[0] === "sessions" ? (
          <ResearchSessions id={decodeURIComponent(path[1] || "")} go={go} />
        ) : path[0] === "knowledge" && path[1] ? (
          <KnowledgePage kind={path[1]} id={decodeURIComponent(path[2] || "")} go={go} />
        ) : (
          <Home go={go} />
        )}
      </main>
    </>
  );
}
