let mockPool: {
  connect: jest.Mock;
  query: jest.Mock;
  end: jest.Mock;
};

let mockClient: {
  query: jest.Mock;
  release: jest.Mock;
};

jest.mock('pg', () => ({
  Pool: jest.fn().mockImplementation(() => ({
    connect: (...args: unknown[]) => mockPool.connect(...args),
    query: (...args: unknown[]) => mockPool.query(...args),
    end: (...args: unknown[]) => mockPool.end(...args),
  })),
}));

const { DatabaseService } = require('./database.service') as typeof import('./database.service');

describe('DatabaseService transaction context', () => {
  beforeEach(() => {
    mockClient = {
      query: jest.fn().mockResolvedValue(undefined),
      release: jest.fn(),
    };
    mockPool = {
      connect: jest.fn().mockResolvedValue(mockClient),
      query: jest.fn(),
      end: jest.fn(),
    };
  });

  it('sets tenant and user context transaction-locally before committing', async () => {
    const service = new DatabaseService();
    const result = await service.withTenant('tenant-1', 'user-1', async (client) => {
      expect(client).toBe(mockClient);
      return 'completed';
    });

    expect(result).toBe('completed');
    expect(mockClient.query.mock.calls).toEqual([
      ['BEGIN'],
      [
        "SELECT set_config('app.tenant_id', $1, true), set_config('app.user_id', $2, true)",
        ['tenant-1', 'user-1'],
      ],
      ['COMMIT'],
    ]);
    expect(mockPool.connect).toHaveBeenCalledTimes(1);
    expect(mockClient.release).toHaveBeenCalledTimes(1);
  });

  it('sets only user context for the membership lookup transaction', async () => {
    const service = new DatabaseService();

    await service.withUser('user-1', async (client) => {
      expect(client).toBe(mockClient);
    });

    expect(mockClient.query.mock.calls).toEqual([
      ['BEGIN'],
      ["SELECT set_config('app.user_id', $1, true)", ['user-1']],
      ['COMMIT'],
    ]);
    expect(mockClient.release).toHaveBeenCalledTimes(1);
  });

  it('rolls back, rethrows the callback error, and releases the client', async () => {
    const service = new DatabaseService();
    const callbackError = new Error('callback failed');

    await expect(
      service.withTenant('tenant-1', 'user-1', async () => {
        throw callbackError;
      }),
    ).rejects.toBe(callbackError);

    expect(mockClient.query.mock.calls.at(-1)).toEqual(['ROLLBACK']);
    expect(mockClient.release).toHaveBeenCalledTimes(1);
  });

  it('rolls back and releases the client when context setup fails', async () => {
    const service = new DatabaseService();
    const setupError = new Error('context setup failed');
    mockClient.query.mockResolvedValueOnce(undefined).mockRejectedValueOnce(setupError);

    await expect(
      service.withTenant('tenant-1', 'user-1', async () => 'unreachable'),
    ).rejects.toBe(setupError);

    expect(mockClient.query.mock.calls).toEqual([
      ['BEGIN'],
      [
        "SELECT set_config('app.tenant_id', $1, true), set_config('app.user_id', $2, true)",
        ['tenant-1', 'user-1'],
      ],
      ['ROLLBACK'],
    ]);
    expect(mockClient.release).toHaveBeenCalledTimes(1);
  });
});
