import { Controller, Get, ServiceUnavailableException } from '@nestjs/common';
import { DatabaseService } from '../common/database.service';

@Controller('health')
export class HealthController {
  constructor(private readonly db: DatabaseService) {}

  @Get('live')
  live() {
    return { success: true, data: { status: 'live' }, meta: { timestamp: new Date().toISOString() } };
  }

  @Get('ready')
  async ready() {
    try {
      await this.db.ping();
      return { success: true, data: { status: 'ready', dependencies: { postgres: 'ok' } } };
    } catch {
      throw new ServiceUnavailableException({ success: false, error: { code: 'DEPENDENCY_UNAVAILABLE', message: 'Database readiness check failed' } });
    }
  }
}
