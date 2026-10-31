import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QualityDurationWarning } from './QualitySettings';
import { request } from '../lib/api';

vi.mock('../lib/api', () => ({ request: vi.fn(), api: {} }));
const requestMock = vi.mocked(request);
const metadata = { quality_status: 'warning', quality_warnings: ['cinematic_output_spec'], duration_seconds: 5.258344, width:1088, height:1920 };

describe('cloud video output warning', () => {
  beforeEach(() => requestMock.mockReset());
  it('shows the latest real dimensions and duration, and the preserve-frame assembly method', async () => {
    requestMock.mockResolvedValue({ quality_checks: [
      {check_type:'cinematic_output_spec',status:'failed',details_json:{actual:{frames:0}}},
      {check_type:'cinematic_output_spec',status:'warning',details_json:{actual:{width:1088,height:1920,duration:5.258344,frames:124},expected:{width:1080,height:1920,frames:120,fps:24}}},
    ] });
    render(<QualityDurationWarning metadata={metadata} videoId="v1"/>);
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('实际：尺寸 1088 × 1920；时长 5.258 秒；帧数 未提供');
    await waitFor(() => expect(screen.getByTestId('duration-warning')).toHaveTextContent('请求：尺寸 1080 × 1920；时长 5 秒；帧数 120'));
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('实际：尺寸 1088 × 1920；时长 5.258 秒；帧数 124');
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('等比缩放并补边，保留完整画面');
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('请播放核对画面和声音');
  });
  it('shows only known metadata when detailed checks cannot be fetched', async () => {
    requestMock.mockRejectedValue(new Error('unavailable'));
    render(<QualityDurationWarning metadata={metadata} videoId="v1"/>);
    await waitFor(() => expect(requestMock).toHaveBeenCalled());
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('实际：尺寸 1088 × 1920；时长 5.258 秒；帧数 未提供');
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('请求：尺寸 未提供；时长 未提供；帧数 未提供');
  });
  it('does not invent dimensions or duration and does not show ordinary media warnings', () => {
    const { rerender } = render(<QualityDurationWarning metadata={{ quality_status:'passed' }} videoId="v1"/>);
    expect(screen.queryByTestId('duration-warning')).not.toBeInTheDocument();
    rerender(<QualityDurationWarning metadata={{quality_status:'warning',quality_warnings:['cinematic_output_spec']}}/>);
    expect(screen.getByTestId('duration-warning')).toHaveTextContent('实际：尺寸 未提供；时长 未提供；帧数 未提供');
  });
  it('does not display a stale warning after the latest backend check passes', async () => {
    requestMock.mockResolvedValue({ quality_checks: [{check_type:'cinematic_output_spec',status:'passed',details_json:{}}] });
    render(<QualityDurationWarning metadata={metadata} videoId="v1"/>);
    await waitFor(() => expect(screen.queryByTestId('duration-warning')).not.toBeInTheDocument());
  });
});
