import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '../../test/render';
import type { AnalysisWorkItem } from '../../types/workspace';
import { WorkPlanEditor } from './WorkPlanEditor';

const items: AnalysisWorkItem[] = [
  {
    item_id: 'wi-1',
    analysis_type: 'business',
    evidence_card_ids: ['ev-1', 'ev-2'],
  },
];

describe('WorkPlanEditor 折叠（V1.1 产品文案）', () => {
  it('默认全展开：TextArea 输入可见且可交互', () => {
    const onChange = vi.fn();
    renderWithProviders(<WorkPlanEditor value={items} onChange={onChange} />);
    const textarea = screen.getByLabelText('研究条目 1 证据');
    expect(textarea).toBeVisible();
    fireEvent.change(textarea, { target: { value: 'ev-3, ev-4' } });
    expect(onChange).toHaveBeenCalledWith([{ ...items[0], evidence_card_ids: ['ev-3', 'ev-4'] }]);
  });

  it('点击折叠头收起 → 内容不可见', async () => {
    renderWithProviders(<WorkPlanEditor value={items} onChange={vi.fn()} />);
    const textarea = screen.getByLabelText('研究条目 1 证据');
    expect(textarea).toBeVisible();
    fireEvent.click(screen.getByText('研究条目 1'));
    await waitFor(() => expect(textarea).not.toBeVisible());
  });

  it('disabled 时内容锁定展开且只读', async () => {
    renderWithProviders(<WorkPlanEditor value={items} onChange={vi.fn()} disabled />);
    const textarea = screen.getByLabelText('研究条目 1 证据');
    expect(textarea).toBeVisible();
    expect(textarea).toBeDisabled();
    // collapsible="disabled"：点击折叠头不改变展开状态。
    fireEvent.click(screen.getByText('研究条目 1'));
    await waitFor(() => expect(textarea).toBeVisible());
  });

  it('删除按钮位于折叠头 extra，点击删除整项', () => {
    const onChange = vi.fn();
    renderWithProviders(<WorkPlanEditor value={items} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: '删除研究条目 1' }));
    expect(onChange).toHaveBeenCalledWith([]);
  });
});
