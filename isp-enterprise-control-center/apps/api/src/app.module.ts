import { Module } from '@nestjs/common';
import { HealthController } from './health/health.controller';
import { DatabaseService } from './common/database.service';

@Module({ controllers: [HealthController], providers: [DatabaseService], exports: [DatabaseService] })
export class AppModule {}
