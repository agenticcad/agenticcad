// ribbon replica shared by the landing page and the tutorial player (icons mirror the app's ribbon)
export const ICONS = {
  box: `<svg viewBox="0 0 24 24"><path d="M12 3 20 7.5v9L12 21l-8-4.5v-9L12 3Z"/><path d="M12 12 20 7.5M12 12v9M12 12 4 7.5"/></svg>`,
  cyl: `<svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/></svg>`,
  sph: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><ellipse cx="12" cy="12" rx="8.5" ry="3.2"/><path d="M12 3.5v17"/></svg>`,
  sketch: `<svg viewBox="0 0 24 24"><path d="M4 5h16v14H4z"/><circle cx="15" cy="13" r="2.6"/><path d="M7 9h5"/></svg>`,
  pull: `<svg viewBox="0 0 24 24"><path d="M4 20h16M6 20v-5h12v5"/><path d="M12 15V4M8.5 7.5 12 4l3.5 3.5"/></svg>`,
  hole: `<svg viewBox="0 0 24 24"><path d="M3 10h18v9H3z"/><path d="M9 10v9M15 10v9"/><ellipse cx="12" cy="10" rx="3" ry="1.2"/><path d="M12 3v5"/></svg>`,
  fillet: `<svg viewBox="0 0 24 24"><path d="M4 20V10a6 6 0 0 1 6-6h10"/><path d="M4 20h16V4" stroke-dasharray="2 2" opacity=".5"/></svg>`,
  chamfer: `<svg viewBox="0 0 24 24"><path d="M4 20V11l7-7h9"/><path d="M4 20h16V4" stroke-dasharray="2 2" opacity=".5"/></svg>`,
  shell: `<svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><path d="M8 8h8v12H8z"/></svg>`,
  move: `<svg viewBox="0 0 24 24"><path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9 3 12l3 3M18 9l3 3-3 3"/></svg>`,
  measure: `<svg viewBox="0 0 24 24"><path d="M3 17 17 3l4 4L7 21H3z"/><path d="m13 7 2 2M10 10l2 2M7 13l2 2"/></svg>`,
};
export const RIBBON = [
  { name: 'Create', tools: [
    { id: 'box', icon: 'box', label: 'Box', key: 'B', tip: 'Box — click a face (or the grid) to place a box on it' },
    { id: 'cylinder', icon: 'cyl', label: 'Cylinder', key: 'C', tip: 'Cylinder — click a face to place its base' },
    { id: 'sphere', icon: 'sph', label: 'Sphere', key: 'O', tip: 'Sphere — click a face to place it' },
    { id: 'sketch', icon: 'sketch', label: 'Sketch', key: 'K', tip: 'Sketch — click a planar face, then draw' } ] },
  { name: 'Modify', tools: [
    { id: 'extrude', icon: 'pull', label: 'Press/Pull', key: 'Q', tip: 'Press/Pull — click a planar face, drag the handle or type a distance' },
    { id: 'hole', icon: 'hole', label: 'Hole', key: 'H', tip: 'Hole — click where it goes; plain, counterbored or tapped' },
    { id: 'fillet', icon: 'fillet', label: 'Fillet', key: 'F', tip: 'Fillet — click edges or a whole face, then a radius' },
    { id: 'chamfer', icon: 'chamfer', label: 'Chamfer', key: 'X', tip: 'Chamfer — click edges or a face, then a length' },
    { id: 'shell', icon: 'shell', label: 'Shell', key: 'L', tip: 'Shell — click the faces to open, then a wall thickness' },
    { id: 'move', icon: 'move', label: 'Move', key: 'V', tip: 'Move — pick a body, then translate / rotate' } ] },
  { name: 'Inspect', tools: [
    { id: 'measure', icon: 'measure', label: 'Measure', key: 'I', tip: 'Measure — snaps to vertices, edges and faces; two picks give distance and angle' } ] },
];
