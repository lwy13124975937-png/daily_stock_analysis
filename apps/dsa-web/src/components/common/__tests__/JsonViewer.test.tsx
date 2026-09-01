import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { JsonViewer } from '../JsonViewer';


describe('JsonViewer', () => {
  it('renders provider strings as text instead of executable HTML', () => {
    const payload = '<img src=x onerror="window.__pwned=true"><script>alert(1)</script>';
    const { container } = render(<JsonViewer data={{ payload }} />);

    expect(screen.getByText(/<img src=x onerror=/)).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });
});
