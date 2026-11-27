import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/context';
import { useTheme } from '../theme/context';

/** Shell applicativa: barra superiore con navigazione, tema e logout. */
export default function Layout() {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();

  const handleLogout = () => {
    logout();
    navigate('/login', { replace: true });
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <NavLink to="/chat" className="brand">
          Virtualis<span>.</span>
        </NavLink>
        <nav aria-label="Navigazione principale">
          <NavLink to="/chat" className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
            Chat
          </NavLink>
          <NavLink to="/search" className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
            Ricerca
          </NavLink>
          <NavLink
            to="/documents"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            Documenti
          </NavLink>
          {user?.role === 'admin' && (
            <NavLink to="/m365" className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
              Microsoft 365
            </NavLink>
          )}
          <NavLink
            to="/knowledge"
            className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
          >
            Knowledge Graph
          </NavLink>
          {user?.role === 'admin' && (
            <NavLink to="/admin" className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
              Amministrazione
            </NavLink>
          )}
        </nav>
        <div className="user-area">
          <span>
            {user?.username}
            {user?.role === 'admin' && <span className="faint"> · admin</span>}
          </span>
          <button
            type="button"
            className="icon-btn"
            onClick={toggleTheme}
            aria-label={theme === 'dark' ? 'Passa al tema chiaro' : 'Passa al tema scuro'}
            title={theme === 'dark' ? 'Tema chiaro' : 'Tema scuro'}
          >
            {theme === 'dark' ? '☀' : '☾'}
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={handleLogout}>
            Esci
          </button>
        </div>
      </header>
      <Outlet />
    </div>
  );
}
